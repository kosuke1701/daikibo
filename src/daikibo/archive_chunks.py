"""Chunked portable knowledge history, with bounded-memory standalone inspection.

This is a historical data format, never an executable backup or new review evidence.
JSONL records and raw byte chunks are independently content-addressed. A temporary
SQLite index validates cross-record references without building the whole graph in RAM.
"""
from __future__ import annotations

import codecs
import hashlib
import os
import sqlite3
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from .backup_artifacts import LEAF_BYTES, artifact_size, iter_artifact_range, open_artifact_session
from .common import Fault, canonical, digest, need, parse_json
from .portable_context import (
    CONTEXT_SECTIONS, TRACEABILITY_SECTIONS, candidate_context_row,
    context_queries, row_cas_refs,
)
from .program_origins import validate_origin_rows, validate_origin_store

FORMAT = 'daikibo.knowledge-snapshot.v3'
ARCHIVE_FORMAT = 'daikibo.knowledge-archive.v3'
WORKSTREAM_FORMAT = 'daikibo.knowledge-snapshot.v4'
RETURN_FORMAT = 'daikibo.knowledge-snapshot.v5'
SUBPLAN_FORMAT = 'daikibo.knowledge-snapshot.v6'
LOCAL_EXECUTION_FORMAT = 'daikibo.knowledge-snapshot.v7'
EXECUTION_CONTROL_FORMAT = 'daikibo.knowledge-snapshot.v8'
COLLECTOR_FAILURE_FORMAT = 'daikibo.knowledge-snapshot.v9'
TRACEABILITY_FORMAT = 'daikibo.knowledge-snapshot.v10'
TRACEABILITY_ARCHIVE_FORMAT = 'daikibo.knowledge-archive.v10'
ASSURANCE_FORMAT = 'daikibo.knowledge-snapshot.v11'
ASSURANCE_ARCHIVE_FORMAT = 'daikibo.knowledge-archive.v11'
ORIGIN_FORMAT = 'daikibo.knowledge-snapshot.v12'
ORIGIN_ARCHIVE_FORMAT = 'daikibo.knowledge-archive.v12'
DOMAIN_FORMAT = 'daikibo.knowledge-snapshot.v13'
DOMAIN_ARCHIVE_FORMAT = 'daikibo.knowledge-archive.v13'
FORMATS = {'daikibo.knowledge-snapshot.v2', FORMAT, WORKSTREAM_FORMAT, RETURN_FORMAT, SUBPLAN_FORMAT, LOCAL_EXECUTION_FORMAT, EXECUTION_CONTROL_FORMAT, COLLECTOR_FAILURE_FORMAT, TRACEABILITY_FORMAT, ASSURANCE_FORMAT, ORIGIN_FORMAT, DOMAIN_FORMAT}
ARCHIVE_FORMATS = {'daikibo.knowledge-archive.v2', ARCHIVE_FORMAT, 'daikibo.knowledge-archive.v4', 'daikibo.knowledge-archive.v5', 'daikibo.knowledge-archive.v6', 'daikibo.knowledge-archive.v7', 'daikibo.knowledge-archive.v8', 'daikibo.knowledge-archive.v9', TRACEABILITY_ARCHIVE_FORMAT, ASSURANCE_ARCHIVE_FORMAT, ORIGIN_ARCHIVE_FORMAT, DOMAIN_ARCHIVE_FORMAT}
CHUNK_BYTES = 1024 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024 * 1024
V2_SECTIONS = ('project_record','artifacts','revisions','sources','dispositions','links',
            'decisions','changes','conflicts','documents','attempts','programs','breakdowns',
            'packets','members','adoptions','closures','review_scopes','uploads','upload_units','upload_batches')
SECTIONS = V2_SECTIONS + ('task_revision_proposals','task_revision_history')

WORKSTREAM_SECTIONS = ('workstreams','workstream_packets','workstream_records')

RETURN_SECTIONS = ('scope_returns','scope_return_packets')
SUBPLAN_SECTIONS = ('subplans','subplan_packets','subplan_compositions')
LOCAL_EXECUTION_SECTIONS = ('local_execution_proposals','local_execution_packets','local_execution_records')
EXECUTION_CONTROL_SECTIONS = ('tasks','execution_attempts','attempt_assessments',
                               'execution_control_proposals','execution_control_packets',
                               'execution_control_events','execution_control_authorizations')
COLLECTOR_FAILURE_SECTIONS = ('collector_failure_history',)
# Unit B target refs may point at a Task candidate.  These ordinary runtime
# rows are retained as an archive context section so the portable validator
# can check candidate/task identity and snapshot digests without treating the
# historical archive as executable runtime state.
TRACEABILITY_CONTEXT_SECTIONS = CONTEXT_SECTIONS
ASSURANCE_SECTIONS = ('assurance_objects','assurance_events','assurance_heads','assurance_refs')
ORIGIN_SECTIONS = ('program_origins',)

def sections_for(manifest):
    if manifest['format'] in {TRACEABILITY_FORMAT, ASSURANCE_FORMAT, ORIGIN_FORMAT, DOMAIN_FORMAT}:
        features = manifest.get('features') or {}
        result = list(SECTIONS)
        if features.get('workstreams'): result.extend(WORKSTREAM_SECTIONS)
        if features.get('returns'): result.extend(RETURN_SECTIONS)
        if features.get('subplans'): result.extend(SUBPLAN_SECTIONS)
        if features.get('local_executions'): result.extend(LOCAL_EXECUTION_SECTIONS)
        if features.get('execution_controls'): result.extend(EXECUTION_CONTROL_SECTIONS)
        if features.get('collector_recovery'): result.extend(COLLECTOR_FAILURE_SECTIONS)
        if features.get('traceability') or manifest['format'] == TRACEABILITY_FORMAT:
            result.extend(TRACEABILITY_SECTIONS)
        if features.get('traceability') or features.get('assurance') or manifest['format'] == TRACEABILITY_FORMAT:
            result.extend(TRACEABILITY_CONTEXT_SECTIONS)
        if features.get('assurance') or manifest['format'] == ASSURANCE_FORMAT:
            result.extend(ASSURANCE_SECTIONS)
        if features.get('program_origins') or manifest['format'] == ORIGIN_FORMAT:
            result.extend(ORIGIN_SECTIONS)
        return tuple(result)
    if manifest['format']==COLLECTOR_FAILURE_FORMAT:
        return (SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS +
                LOCAL_EXECUTION_SECTIONS + EXECUTION_CONTROL_SECTIONS + COLLECTOR_FAILURE_SECTIONS)
    if manifest['format']==EXECUTION_CONTROL_FORMAT:return SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS + LOCAL_EXECUTION_SECTIONS + EXECUTION_CONTROL_SECTIONS
    if manifest['format']==LOCAL_EXECUTION_FORMAT:return SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS + LOCAL_EXECUTION_SECTIONS
    if manifest['format']==SUBPLAN_FORMAT:return SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS
    if manifest['format']==RETURN_FORMAT:return SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS
    if manifest['format']==WORKSTREAM_FORMAT:return SECTIONS + WORKSTREAM_SECTIONS
    return SECTIONS if manifest['format']==FORMAT else V2_SECTIONS

def archive_format(manifest):
    if manifest['format']==DOMAIN_FORMAT:return DOMAIN_ARCHIVE_FORMAT
    if manifest['format']==ORIGIN_FORMAT:return ORIGIN_ARCHIVE_FORMAT
    if manifest['format']==ASSURANCE_FORMAT:return ASSURANCE_ARCHIVE_FORMAT
    if manifest['format']==TRACEABILITY_FORMAT:return TRACEABILITY_ARCHIVE_FORMAT
    if manifest['format']==COLLECTOR_FAILURE_FORMAT:return 'daikibo.knowledge-archive.v9'
    if manifest['format']==EXECUTION_CONTROL_FORMAT:return 'daikibo.knowledge-archive.v8'
    if manifest['format']==LOCAL_EXECUTION_FORMAT:return 'daikibo.knowledge-archive.v7'
    if manifest['format']==SUBPLAN_FORMAT:return 'daikibo.knowledge-archive.v6'
    if manifest['format']==RETURN_FORMAT:return 'daikibo.knowledge-archive.v5'
    if manifest['format']==WORKSTREAM_FORMAT:return 'daikibo.knowledge-archive.v4'
    return ARCHIVE_FORMAT if manifest['format']==FORMAT else 'daikibo.knowledge-archive.v2'



def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def queries(workstreams=False,returns=False,subplans=False,local_executions=False,execution_controls=False,traceability=False,assurance=False,program_origins=False):
    """Every query consumes the same project and the caller's single read transaction."""
    values={s:f'SELECT * FROM {s} WHERE project=? ORDER BY id' for s in
            ('artifacts','sources','decisions','changes','conflicts','documents','programs','breakdowns','review_scopes','task_revision_proposals','task_revision_history')}
    values.update({
        'project_record':'SELECT * FROM projects WHERE id=?',
        'revisions':'SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=? ORDER BY r.artifact,r.revision',
        'dispositions':'SELECT d.* FROM dispositions d JOIN sources s ON s.id=d.source WHERE s.project=? ORDER BY d.source,d.start',
        'links':'SELECT l.* FROM links l JOIN artifacts a ON a.id=l.source WHERE a.project=? ORDER BY l.source,l.target,l.relation',
        'attempts':'SELECT a.* FROM attempts a JOIN changes c ON c.id=a.change_id WHERE c.project=? ORDER BY a.id',
        'packets':'SELECT * FROM breakdown_packets WHERE project=? ORDER BY id',
        'members':'SELECT m.* FROM breakdown_members m JOIN breakdowns b ON b.id=m.breakdown WHERE b.project=? ORDER BY m.breakdown,m.ordinal',
        'adoptions':'SELECT a.* FROM breakdown_adoptions a JOIN breakdowns b ON b.id=a.breakdown WHERE b.project=? ORDER BY a.breakdown',
        'closures':'SELECT * FROM program_closures WHERE project=? ORDER BY id',
        'uploads':'SELECT * FROM breakdown_uploads WHERE project=? ORDER BY id',
        'upload_units':'SELECT u.* FROM breakdown_upload_units u JOIN breakdown_uploads b ON b.id=u.upload WHERE b.project=? ORDER BY u.upload,u.ordinal',
        'upload_batches':'SELECT u.* FROM breakdown_upload_batches u JOIN breakdown_uploads b ON b.id=u.upload WHERE b.project=? ORDER BY u.upload,u.revision',
    })
    if workstreams:
        values.update({
            'workstreams':'SELECT * FROM workstreams WHERE project=? ORDER BY id',
            'workstream_packets':'SELECT p.* FROM workstream_packets p JOIN workstreams w ON w.id=p.scope WHERE w.project=? ORDER BY p.scope,p.ordinal',
            'workstream_records':'SELECT * FROM workstream_records WHERE project=? ORDER BY scope,created,id',
        })
    if returns:
        values.update({'scope_returns':'SELECT * FROM scope_returns WHERE project=? ORDER BY id',
                       'scope_return_packets':'SELECT * FROM scope_return_packets WHERE project=? ORDER BY proposal,level,ordinal,id'})
    if subplans:
        values.update({'subplans':'SELECT * FROM subplans WHERE project=? ORDER BY id',
                       'subplan_packets':'SELECT * FROM subplan_packets WHERE project=? ORDER BY subplan,ordinal',
                       'subplan_compositions':'SELECT * FROM subplan_compositions WHERE project=? ORDER BY subplan,created,id'})
    if local_executions:
        values.update({'local_execution_proposals':'SELECT * FROM local_execution_proposals WHERE project=? ORDER BY id',
                       'local_execution_packets':'SELECT * FROM local_execution_packets WHERE project=? ORDER BY proposal,ordinal',
                       'local_execution_records':'SELECT * FROM local_execution_records WHERE project=? ORDER BY proposal,created,id'})
    if execution_controls:
        values.update({
            'tasks':'SELECT * FROM tasks WHERE project=? ORDER BY id',
            'execution_attempts':'SELECT * FROM execution_attempts WHERE project=? ORDER BY task,attempt_epoch,id',
            'attempt_assessments':'SELECT * FROM attempt_assessments WHERE project=? ORDER BY task,attempt_epoch,id',
            'execution_control_proposals':'SELECT * FROM execution_control_proposals WHERE project=? ORDER BY task,created,id',
            'execution_control_packets':'SELECT * FROM execution_control_packets WHERE project=? ORDER BY proposal,ordinal,id',
            'execution_control_events':'SELECT * FROM execution_control_events WHERE project=? ORDER BY proposal,created,id',
            'execution_control_authorizations':'SELECT * FROM execution_control_authorizations WHERE project=? ORDER BY task,control_revision,id',
        })
    if traceability or assurance:
        if traceability:
            values.update({section: f'SELECT * FROM {section} WHERE project=? ORDER BY id'
                           for section in TRACEABILITY_SECTIONS})
        values.update(context_queries())
    if assurance:
        values.update({
            'assurance_objects':'SELECT * FROM assurance_objects WHERE project=? ORDER BY id',
            'assurance_events':'SELECT * FROM assurance_events WHERE project=? ORDER BY id',
            'assurance_heads':'SELECT * FROM assurance_heads WHERE project=? ORDER BY logical_id',
            'assurance_refs':'SELECT r.* FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE o.project=? ORDER BY r.object_id,r.ordinal',
        })
    if program_origins:
        values['program_origins']='SELECT * FROM program_origins WHERE project=? ORDER BY program'
    return values


def decoded(row):
    result=dict(row)
    for key in ('body','config','refs','result'):
        if result.get(key) is not None and isinstance(result[key],str):
            result[key]=parse_json(result[key],limit=MAX_RECORD_BYTES)
    return result


def decoded_section(section,row):
    """Decode JSON columns used by schema-13 execution-control projections."""
    result=decoded(row)
    if section in {'attempt_assessments','execution_control_authorizations'}:
        # ``evidence`` is a JSON column.  ``assessment`` is an enum in the
        # schema-13 authorization table, so decoding it as JSON would turn a
        # perfectly valid ``progress`` value into an invalid-json error.
        if result.get('evidence') is not None and isinstance(result['evidence'],str):
            result['evidence']=parse_json(result['evidence'],limit=MAX_RECORD_BYTES)
    return result


class Builder:
    def __init__(self,store,chunk_bytes=CHUNK_BYTES):
        need(type(chunk_bytes) is int and 1024<=chunk_bytes<=MAX_CHUNK_BYTES,'invalid_chunk_size','Chunk bytes must be 1024..8MiB')
        self.s,self.size=store,chunk_bytes
        self.objects={};self.parts=[];self.buffer=bytearray();self.stream_hash=hashlib.sha256();self.bytes=0

    def put(self,data):
        h=self.s.blob_put(data);self.objects[h]={'bytes':len(data)}
        return {'sha256':h,'bytes':len(data)}

    def raw(self,h):
        parts=[];hashed=hashlib.sha256();staged=[]
        # Publishing each derived chunk changes the shared blob namespace and
        # therefore advances the session generation.  Stage bounded chunks on
        # disk while the source session is open, then publish them after the
        # verified read has closed; this preserves one immutable source handle
        # for the complete stream without weakening invalidation.
        with tempfile.TemporaryDirectory(prefix='artifact-chunks-', dir=self.s.home) as temp:
            with open_artifact_session(self.s,h) as session:
                total=session.size;offset=0;ordinal=0
                while offset < total:
                    target=min(self.size,total-offset);buffer=bytearray()
                    while len(buffer) < target:
                        piece=session.read_range(offset+len(buffer),min(LEAF_BYTES,target-len(buffer)))
                        need(piece,'integrity_error','Artifact ended while splitting')
                        buffer.extend(piece)
                    block=bytes(buffer)
                    need(block,'integrity_error','Artifact ended while splitting')
                    hashed.update(block);offset+=len(block)
                    staged_path=Path(temp)/f'{ordinal:08d}.chunk';staged_path.write_bytes(block)
                    staged.append(staged_path);ordinal+=1
                need(hashed.hexdigest()==h,'integrity_error','Original content changed while splitting')
            for staged_path in staged:
                parts.append(self.put(staged_path.read_bytes()))
        return {'sha256':h,'bytes':total,'chunks':parts}

    def _recovery_raw(self, summary):
        """Collect the durable recovery object graph without omitting leaves."""
        references=[];seen=set()

        def add(blob, kind):
            need(isinstance(blob,str) and len(blob)==64,
                 'integrity_error','Recovery reference is not a blob digest')
            if blob in seen:return
            seen.add(blob)
            raw=self.raw(blob);references.append({'kind':kind,**raw})
            if kind != 'manifest':return
            manifest=parse_json(self.s.blob_get(blob),limit=MAX_MANIFEST_BYTES)
            need(manifest.get('format')=='failed-artifacts.v1',
                 'integrity_error','Recovery manifest has an unsupported format')
            pages=list(manifest.get('entry_pages',[])) if isinstance(manifest.get('entry_pages'),list) else []
            index=manifest.get('entry_page_index') or {}
            index_blob=index.get('blob')
            index_seen=set()
            while index_blob:
                need(index_blob not in index_seen,'integrity_error','Recovery entry index contains a cycle')
                index_seen.add(index_blob)
                add(index_blob,'entry_page_index')
                index_body=parse_json(self.s.blob_get(index_blob),limit=4*1024*1024)
                need(index_body.get('format')=='failed-artifact-entry-index.v1',
                     'integrity_error','Recovery entry index has an unsupported format')
                pages.extend(index_body.get('pages',[]))
                index_blob=index_body.get('previous')
            for page in pages:
                add(page.get('blob'),'entry_page')
                page_body=parse_json(self.s.blob_get(page['blob']),limit=4*1024*1024)
                need(page_body.get('format')=='failed-artifact-entry-page.v1',
                     'integrity_error','Recovery entry page has an unsupported format')
                for chunk in page_body.get('chunks',[]):
                    add(chunk.get('blob'),'entry_chunk')
                    chunk_body=parse_json(self.s.blob_get(chunk['blob']),limit=16*1024*1024)
                    need(chunk_body.get('format')=='failed-artifact-entry-chunk.v1',
                         'integrity_error','Recovery entry chunk has an unsupported format')
                    for entry in chunk_body.get('entries',[]):
                        if entry.get('blob'):
                            add(entry['blob'],'artifact')
        manifest_blob=summary.get('manifest_blob') if isinstance(summary,dict) else None
        if manifest_blob:add(manifest_blob,'manifest')
        return references

    def recovery_record(self, row):
        value={'section':'collector_failure_history','row':row}
        summary=row.get('recovery_artifacts')
        if isinstance(summary,dict):
            raw=self._recovery_raw(summary)
            if raw:value['raw_objects']=raw
        encoded=canonical(value)+b'\n'
        need(len(encoded)<=MAX_RECORD_BYTES,'archive_record_too_large','A recovery history record exceeds the explicit per-record limit')
        self.stream_hash.update(encoded);self.bytes+=len(encoded)
        for pos in range(0,len(encoded),self.size):
            self.buffer.extend(encoded[pos:pos+self.size])
            while len(self.buffer)>=self.size:
                self.parts.append(self.put(bytes(self.buffer[:self.size])));del self.buffer[:self.size]

    def record(self,section,row):
        value={'section':section,'row':decoded_section(section,row)}
        if section in {'sources', 'documents'}:
            refs = sorted(row_cas_refs(section, value['row'], self.s))
            need(len(refs) == 1, 'invalid_archive',
                 'Source/document row does not have exactly one typed CAS object',
                 {'section': section, 'id': value['row'].get('id')})
            value['raw'] = self.raw(refs[0])
        elif section in (*TRACEABILITY_SECTIONS, *TRACEABILITY_CONTEXT_SECTIONS):
            # Traceability history is a typed CAS closure.  Every historical
            # row, including failed staging checkpoints, carries the exact
            # source/tree/Git-object refs needed after the original Git/home
            # disappears.  Keep raw bytes as chunked objects rather than
            # inlining them into one JSON record.
            if section in TRACEABILITY_CONTEXT_SECTIONS:
                value['row'] = candidate_context_row(section, value['row'])
            refs = sorted(row_cas_refs(section, value['row'], self.s))
            value['raw_objects'] = [self.raw(ref) for ref in refs]
        elif section in ASSURANCE_SECTIONS:
            # Identity digests remain ordinary references unless the exact
            # content-addressed object exists. Material payloads use their
            # complete transitive CAS closure. The same typed enumerator is
            # used by direct specification export.
            refs = sorted(row_cas_refs(section, value['row'], self.s))
            raw_objects = [self.raw(ref) for ref in refs]
            if raw_objects:
                value['raw_objects'] = raw_objects
        encoded=canonical(value)+b'\n'
        need(len(encoded)<=MAX_RECORD_BYTES,'archive_record_too_large','A record exceeds the explicit per-record limit; nothing was omitted')
        self.stream_hash.update(encoded);self.bytes+=len(encoded)
        for pos in range(0,len(encoded),self.size):
            self.buffer.extend(encoded[pos:pos+self.size])
            while len(self.buffer)>=self.size:
                self.parts.append(self.put(bytes(self.buffer[:self.size])));del self.buffer[:self.size]

    def finish(self,baseline_id,baseline,counts,features=None):
        if self.buffer:self.parts.append(self.put(bytes(self.buffer)));self.buffer.clear()
        from .domain_responsibility import store_required_contracts
        contracts = store_required_contracts(self.s, baseline['project'])
        manifest={'format':DOMAIN_FORMAT if contracts else ORIGIN_FORMAT if features and features.get('program_origins') else ASSURANCE_FORMAT if features and features.get('assurance') else TRACEABILITY_FORMAT if features and features.get('traceability') else COLLECTOR_FAILURE_FORMAT if 'collector_failure_history' in counts else EXECUTION_CONTROL_FORMAT if 'execution_attempts' in counts else LOCAL_EXECUTION_FORMAT if 'local_execution_proposals' in counts else SUBPLAN_FORMAT if 'subplans' in counts else RETURN_FORMAT if 'scope_returns' in counts else WORKSTREAM_FORMAT if 'workstreams' in counts else FORMAT,'baseline_id':baseline_id,'project':baseline['project'],
                  'baseline':baseline,'baseline_digest':digest(baseline),'chunk_bytes':self.size,
                  'records':{'chunks':self.parts,'bytes':self.bytes,'sha256':self.stream_hash.hexdigest(),'counts':dict(counts)},
                  'objects':self.objects,'generated_do_not_edit':True,'runtime_restore_supported':False,
                  'fresh_test_or_review_evidence':False}
        if contracts:
            manifest['required_contracts'] = contracts
        if features:
            manifest['features'] = dict(features)
        need(len(canonical(manifest))<=MAX_MANIFEST_BYTES,'archive_manifest_too_large','Manifest exceeds explicit bound; no partial archive accepted')
        need(sum(v['bytes'] for v in self.objects.values())<=MAX_ARCHIVE_BYTES,'archive_capacity','Archive exceeds supported disk capacity')
        return manifest


def _recovery_records(store, project):
    """Yield historical recovery references; raw pending bytes stay backup-only."""
    for row in store.all('SELECT id,run,project,subject,role,binding,body FROM receipts WHERE project=? ORDER BY id',(project,)):
        body=parse_json(row['body'],limit=MAX_RECORD_BYTES)
        summary=body.get('recovery_artifacts')
        if summary is not None:
            yield {'id':'receipt:'+row['id'],'project':project,'run':row['run'],'kind':'receipt',
                   'receipt':row['id'],'subject':row['subject'],'role':row['role'],'binding':row['binding'],
                   'recovery_artifacts':summary,'runtime_restore_supported':False}
    for row in store.all("SELECT seq,project,kind,actor,body FROM events WHERE project=? AND kind IN ('retention_reconciled','retention_pending') ORDER BY seq",(project,)):
        body=parse_json(row['body'],limit=MAX_RECORD_BYTES)
        summary=body.get('recovery_artifacts')
        if summary is not None or body.get('run'):
            yield {'id':'event:'+str(row['seq']),'project':project,'run':body.get('run'),
                   'kind':'event','event_kind':row['kind'],'actor':row['actor'],
                   'body':body,'recovery_artifacts':summary,'runtime_restore_supported':False}
    pending=store.home/'recovery'/'pending'
    if pending.is_dir():
        for path in sorted(pending.glob('*.json')):
            if path.is_symlink() or not path.is_file():continue
            try:marker=parse_json(path.read_bytes(),limit=2*1024*1024)
            except Exception:continue
            if not isinstance(marker,dict) or marker.get('project')!=project:continue
            yield {'id':'pending:'+str(marker.get('run',path.stem)),'project':project,
                   'run':marker.get('run'),'kind':'pending_marker','marker':marker,
                   'recovery_artifacts':marker.get('summary'),'portable_raw_included':False,
                   'portable_raw_exclusion':'Pending raw bytes belong to the confidential operational backup; this historical archive retains the marker and durable blobs only.',
                   'runtime_restore_supported':False}


def build(store,project,baseline_id,baseline,chunk_bytes=CHUNK_BYTES,execution_controls=False,collector_recovery=False,traceability=False,assurance=False,program_origins=False):
    validate_origin_store(store)
    planned = bool(store.one('SELECT 1 FROM subplans WHERE project=? LIMIT 1',(project,)))
    delegated = planned or bool(store.one('SELECT 1 FROM workstreams WHERE project=? LIMIT 1',(project,)))
    returns = planned or bool(store.one('SELECT 1 FROM scope_returns WHERE project=? LIMIT 1',(project,)))
    local_executions = bool(store.one('SELECT 1 FROM local_execution_proposals WHERE project=? LIMIT 1',(project,)))
    traceability = bool(traceability or store.one("SELECT 1 FROM traceability_sets WHERE project=? LIMIT 1",(project,)))
    from .domain_responsibility import store_required_contracts
    assurance = bool(store_required_contracts(store, project)) or bool(assurance or store.one("SELECT 1 FROM assurance_objects WHERE project=? LIMIT 1",(project,)))
    program_origins = True
    recovery_available=collector_recovery or bool(store.one("SELECT 1 FROM receipts WHERE project=? AND json_extract(body,'$.recovery_artifacts') IS NOT NULL LIMIT 1",(project,))) or bool(store.one("SELECT 1 FROM events WHERE project=? AND kind IN ('retention_reconciled','retention_pending') LIMIT 1",(project,)))
    builder=Builder(store,chunk_bytes);counts=Counter({s:0 for s in (SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS if planned else SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS if returns else SECTIONS + WORKSTREAM_SECTIONS if delegated else SECTIONS)})
    if local_executions:
        counts=Counter({s:0 for s in (SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS + LOCAL_EXECUTION_SECTIONS)})
    if execution_controls:
        counts=Counter({s:0 for s in (SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS + LOCAL_EXECUTION_SECTIONS + EXECUTION_CONTROL_SECTIONS)})
    if recovery_available:
        counts=Counter({s:0 for s in (SECTIONS + WORKSTREAM_SECTIONS + RETURN_SECTIONS + SUBPLAN_SECTIONS + LOCAL_EXECUTION_SECTIONS + EXECUTION_CONTROL_SECTIONS + COLLECTOR_FAILURE_SECTIONS)})
    if traceability or assurance or program_origins:
        counts=Counter({s:0 for s in (SECTIONS + (WORKSTREAM_SECTIONS if delegated else ()) + (RETURN_SECTIONS if returns else ()) + (SUBPLAN_SECTIONS if planned else ()) + (LOCAL_EXECUTION_SECTIONS if local_executions else ()) + (EXECUTION_CONTROL_SECTIONS if execution_controls else ()) + (COLLECTOR_FAILURE_SECTIONS if recovery_available else ()) + (TRACEABILITY_SECTIONS if traceability else ()) + (TRACEABILITY_CONTEXT_SECTIONS if traceability or assurance else ()) + (ASSURANCE_SECTIONS if assurance else ()))})
        if program_origins:
            counts.update({section:0 for section in ORIGIN_SECTIONS})
    features={'traceability':traceability,'assurance':assurance,'program_origins':program_origins,'workstreams':delegated,'returns':returns,'subplans':planned,
              'local_executions':local_executions,'execution_controls':execution_controls,
              'collector_recovery':recovery_available}
    for section,sql in queries(delegated,returns,planned,local_executions,execution_controls,traceability,assurance,program_origins).items():
        # The caller holds a Store transaction throughout this snapshot.
        cursor=store.execute(sql,(project,))
        for row in cursor:
            builder.record(section,dict(row));counts[section]+=1
    if recovery_available:
        for row in _recovery_records(store,project):
            builder.recovery_record(row);counts['collector_failure_history']+=1
    return builder.finish(baseline_id,baseline,counts,features if traceability or assurance or program_origins else None)


def checked_chunks(manifest,parts,read,used):
    need(isinstance(parts,list),'invalid_archive','Chunk sequence must be a list')
    for part in parts:
        h=part['sha256'];length=part['bytes']
        need(type(length) is int and 0<length<=MAX_CHUNK_BYTES and manifest['objects'].get(h)=={'bytes':length},
             'invalid_archive','Chunk is missing, oversized or has an inconsistent length')
        data=read(h)
        need(len(data)==length and digest(data)==h,'invalid_archive','Chunk checksum/size differs',h)
        used.add(h);yield data


def records(manifest,read,used):
    remaining=bytearray();hash_value=hashlib.sha256();total=0
    for block in checked_chunks(manifest,manifest['records']['chunks'],read,used):
        total+=len(block);hash_value.update(block);remaining.extend(block)
        start=0
        while True:
            end=remaining.find(b'\n',start)
            if end<0:break
            need(end-start+1<=MAX_RECORD_BYTES,'invalid_archive','Record too large')
            yield parse_json(bytes(remaining[start:end]),limit=MAX_RECORD_BYTES)
            start=end+1
        if start:del remaining[:start]
        need(len(remaining)<=MAX_RECORD_BYTES,'invalid_archive','Unterminated or oversized record')
    need(not remaining and total==manifest['records']['bytes'] and hash_value.hexdigest()==manifest['records']['sha256'],
         'invalid_archive','Record stream is truncated, reordered or corrupted')


def key_for(section,row):
    if section == 'program_origins':
        return row['program']
    if section in {'traceability_sets','traceability_revisions','traceability_items',
                   'traceability_proposals','traceability_decisions','traceability_mappings',
                   'traceability_bindings','traceability_records'}:
        return row['id']
    if section == 'assurance_heads':
        return canonical([row['project'], row['logical_id']]).decode()
    if section == 'assurance_refs':
        return canonical([row['object_id'], row['ordinal']]).decode()
    if section=='revisions':return canonical([row['artifact'],row['revision']]).decode()
    if section=='links':return canonical([row['source'],row['target'],row['relation']]).decode()
    if section=='members':return canonical([row['breakdown'],row['packet']]).decode()
    if section=='adoptions':return row['breakdown']
    if section=='upload_units':return canonical([row['upload'],row['unit']]).decode()
    if section=='upload_batches':return canonical([row['upload'],row['revision']]).decode()
    return row['id']


# Historical source references are projected at read time.  Keep the page
# bounded so a malformed old record cannot make an inspection response
# unbounded; the total and truncation flag make a partial page explicit.
HISTORICAL_REFERENCE_DIAGNOSTIC_PAGE = 100


def _json_type(value):
    """Return the portable JSON type name used by archive diagnostics."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, (int, float)):
        return 'number'
    return type(value).__name__


def _source_id(value):
    """Recognize the current nonempty source-ID shape without coercion."""
    return isinstance(value, str) and bool(value.strip()) and '\x00' not in value


def historical_source_reference_projection(revisions, artifacts, source_exists,
                                           *, error_code='invalid_archive',
                                           diagnostic_page=HISTORICAL_REFERENCE_DIAGNOSTIC_PAGE):
    """Validate current references and project opaque historical references.

    ``source_exists`` is deliberately called only for a well-typed string.  A
    historical object, number, null, empty string, or other malformed element
    remains at its original JSON pointer and is never coerced into a source ID.
    The caller supplies the archive-specific error code so direct snapshots
    and chunked archives expose their established structured failure type.
    """
    if callable(artifacts):
        current_revision = artifacts
    else:
        artifact_rows = {row['id']: row for row in artifacts}

        def current_revision(artifact):
            row = artifact_rows.get(artifact)
            return row.get('revision') if row is not None else None

    diagnostics = []
    total = 0

    def add_diagnostic(row, pointer, value):
        nonlocal total
        total += 1
        if len(diagnostics) >= diagnostic_page:
            return
        diagnostics.append({
            'code': 'historical_reference_uninterpreted',
            'section': 'revisions',
            'artifact': row['artifact'],
            'revision': row['revision'],
            'digest': row['digest'],
            'pointer': pointer,
            'actual_type': _json_type(value),
            'retained': True,
            'reference_validated': False,
            'reason': 'Historical field does not encode a source ID under the current reference contract',
        })

    for row in revisions:
        revision = current_revision(row.get('artifact'))
        if revision is None:
            # Artifact/revision ownership is checked by the surrounding
            # snapshot validator before this helper is called.
            continue
        current = row.get('revision') == revision
        body = row.get('body')
        if not isinstance(body, dict):
            need(False, error_code, 'Historical artifact body is not an object')
        if 'source_refs' not in body:
            continue
        references = body['source_refs']
        if current:
            need(isinstance(references, list) and bool(references), error_code,
                 'Current source_refs must be a nonempty list of source IDs')
            seen = set()
            for source in references:
                need(_source_id(source), error_code,
                     'Current source_refs contains a malformed source ID')
                need(source not in seen, error_code,
                     'Current source_refs contains a duplicate source ID')
                seen.add(source)
                source_exists(source)
            continue
        if not isinstance(references, list):
            add_diagnostic(row, '/source_refs', references)
            continue
        for index, source in enumerate(references):
            if _source_id(source):
                source_exists(source)
            else:
                add_diagnostic(row, f'/source_refs/{index}', source)

    interpretation = 'unresolved' if total else 'resolved'
    return {
        'history_integrity': 'verified',
        'historical_reference_diagnostic_count': total,
        'historical_reference_diagnostics': diagnostics,
        'historical_reference_diagnostics_total': total,
        'historical_reference_diagnostics_truncated': total > len(diagnostics),
        'historical_reference_interpretation': interpretation,
    }


class Inspector:
    """On-disk index. Only a single logical record/raw chunk is decoded at a time."""
    def __init__(self,db,manifest,read):
        self.db,self.m,self.read=db,manifest,read;self.used=set();self.counts=Counter({s:0 for s in sections_for(manifest)});self.history_projection={};self.recovery_raw={};self.trace_raw={};self.trace_blob_bytes={};self.assurance_raw={};self.source_raw={}
        db.execute('PRAGMA journal_mode=OFF');db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA cache_size=-8192');db.execute('PRAGMA temp_store=FILE')
        db.executescript('CREATE TABLE rows(section TEXT,key TEXT,ref TEXT,n INTEGER,data TEXT NOT NULL,PRIMARY KEY(section,key)); CREATE INDEX by_reference ON rows(section,ref,n);')

    def get(self,section,key):
        row=self.db.execute('SELECT data FROM rows WHERE section=? AND key=?',(section,key)).fetchone()
        need(row,'invalid_archive','Dangling record reference',{'section':section,'key':key})
        return parse_json(row[0],limit=MAX_RECORD_BYTES)

    def exists(self,section,key):
        need(self.db.execute('SELECT 1 FROM rows WHERE section=? AND key=?',(section,key)).fetchone(),
             'invalid_archive','Missing referenced record',{'section':section,'key':key})

    def each(self,section,ref=None):
        sql='SELECT data FROM rows WHERE section=?';params=[section]
        if ref is not None:sql+=' AND ref=?';params.append(ref)
        for row in self.db.execute(sql+' ORDER BY ref,n,key',params):yield parse_json(row[0],limit=MAX_RECORD_BYTES)

    def raw(self,record):
        data=record['raw'];section=record['section'];row=record['row']
        expected=row['blob'] if section=='sources' else row['body']['raw_digest']
        self.raw_descriptor(data,expected)
        if section=='sources':
            self.source_raw[expected] = data
            # Source text is decoded incrementally so a large source never
            # becomes one aggregate object during inspection.
            hashed=hashlib.sha256();total=characters=0
            decoder=codecs.getincrementaldecoder('utf-8')('strict')
            for block in checked_chunks(self.m,data['chunks'],self.read,self.used):
                hashed.update(block);total+=len(block);characters+=len(decoder.decode(block))
            characters+=len(decoder.decode(b'',final=True))
            need(total==data['bytes'] and hashed.hexdigest()==expected,'invalid_archive','Raw object is incomplete or reordered')
            need(characters==row['characters'],'invalid_archive','Source character count differs')
            return
        # Documents retain their original byte count in the row body.
        need(data['bytes']==row['body']['bytes'],'invalid_archive','Document size differs')

    def raw_descriptor(self,data,expected=None):
        need(isinstance(data,dict) and isinstance(data.get('sha256'),str),'invalid_archive','Raw object descriptor is malformed')
        expected=expected or data['sha256']
        need(data['sha256']==expected,'invalid_archive','Raw bytes identity differs')
        hashed=hashlib.sha256();total=characters=0
        for block in checked_chunks(self.m,data['chunks'],self.read,self.used):
            hashed.update(block);total+=len(block)
        need(total==data['bytes'] and hashed.hexdigest()==expected,'invalid_archive','Raw object is incomplete or reordered')

    def load(self):
        for record in records(self.m,self.read,self.used):
            section,row=record['section'],record['row']
            need(section in sections_for(self.m) and isinstance(row,dict),'invalid_archive','Unknown section or invalid row')
            need('project' not in row or row['project']==self.m['project'],'invalid_archive','Cross-project record')
            key=key_for(section,row);need(isinstance(key,str),'invalid_archive','Record key must be a string')
            ref=row.get('subplan') if section in {'subplan_packets','subplan_compositions'} else row.get('proposal') if section in {'scope_return_packets','local_execution_packets','local_execution_records','execution_control_packets','execution_control_events'} else row.get('scope') if section in WORKSTREAM_SECTIONS else row.get('task') if section in {'task_revision_history','execution_attempts','attempt_assessments'} else row.get('artifact') if section=='revisions' else row.get('source') if section=='dispositions' else row.get('breakdown') if section=='members' else row.get('upload') if section in {'upload_units','upload_batches'} else row.get('revision') if section in {'traceability_items','traceability_decisions','traceability_mappings','traceability_bindings'} else row.get('set_id') if section in {'traceability_revisions','traceability_proposals'} else None
            n=row.get('to_revision') if section=='task_revision_history' else row.get('revision') if section in {'revisions','upload_batches'} else row.get('start') if section=='dispositions' else row.get('ordinal') if section in {'members','upload_units','workstream_packets','scope_return_packets','subplan_packets','local_execution_packets','execution_control_packets','traceability_items'} else row.get('attempt_epoch') if section in {'execution_attempts','attempt_assessments'} else row.get('control_revision') if section=='execution_control_authorizations' else row.get('created') if section in {'local_execution_records','execution_control_proposals','execution_control_events','execution_control_authorizations','traceability_proposals','traceability_decisions','traceability_mappings','traceability_bindings','traceability_records'} else row.get('revision') if section=='traceability_revisions' else None
            stored_row=dict(row)
            if section=='collector_failure_history':
                # The raw closure is part of the portable record contract;
                # retain it in the temporary index for transitive validation.
                stored_row['raw_objects']=record.get('raw_objects',[])
            self.db.execute('INSERT INTO rows VALUES(?,?,?,?,?)',(section,key,ref,n,canonical(stored_row).decode()))
            if section in {'sources','documents'}:self.raw(record)
            elif section in (*TRACEABILITY_SECTIONS, *TRACEABILITY_CONTEXT_SECTIONS):
                raw_objects=record.get('raw_objects',[])
                need(isinstance(raw_objects,list),'invalid_archive','Traceability raw closure is malformed')
                for raw in raw_objects:
                    self.raw_descriptor(raw)
                    reference=raw.get('sha256')
                    if reference not in self.trace_blob_bytes:
                        self.trace_blob_bytes[reference]=self.raw_bytes(raw)
                refs = set()
                from .traceability import _trace_blob_refs
                refs.update(_trace_blob_refs(row))
                actual={raw.get('sha256') for raw in raw_objects if isinstance(raw,dict)}
                need(refs <= actual,'invalid_archive','Traceability row references a blob absent from the archive',sorted(refs-actual))
                self.trace_raw[key]=actual
            elif section in ASSURANCE_SECTIONS:
                raw_objects=record.get('raw_objects',[])
                need(isinstance(raw_objects,list),'invalid_archive','Assurance raw closure is malformed')
                for raw in raw_objects:
                    self.raw_descriptor(raw)
                    reference=raw.get('sha256')
                    if reference not in self.trace_blob_bytes:
                        self.trace_blob_bytes[reference]=self.raw_bytes(raw)
                self.assurance_raw[key]={raw.get('sha256') for raw in raw_objects if isinstance(raw,dict)}
            elif section=='collector_failure_history':
                raw_objects=record.get('raw_objects',[])
                need(isinstance(raw_objects,list),'invalid_archive','Recovery raw closure is malformed')
                for raw in raw_objects:self.raw_descriptor(raw)
                self.recovery_raw[key]={raw.get('sha256'):raw for raw in raw_objects if isinstance(raw,dict)}
            else:need('raw' not in record and 'raw_objects' not in record,'invalid_archive','Unexpected raw content')
            self.counts[section]+=1
        need(dict(self.counts)==self.m['records']['counts'],'invalid_archive','Section counts differ')
        need(self.used==set(self.m['objects']),'invalid_archive','Manifest contains unreferenced or missing chunks')
        self.db.commit()

    def validate(self):
        self.load();project=self.m['project'];baseline=self.m['baseline']
        need(self.counts['project_record']==1 and self.get('project_record',project)['id']==project,
             'invalid_archive','Project record missing')
        need(baseline['project']==project and digest(baseline)==self.m['baseline_digest'],'invalid_archive','Baseline binding differs')
        from .domain_responsibility import required_contracts, validate_contract_manifest
        contracts = required_contracts(list(self.each('assurance_objects')),
            list(self.each('runs')) + list(self.each('receipts')))
        validate_contract_manifest(self.m.get('required_contracts'), contracts,
            modern=self.m['format']==DOMAIN_FORMAT, code='invalid_archive')
        accepted_count=0
        for row in self.each('artifacts'):
            need(type(row['revision']) is int and row['revision']>0 and digest(row['body'])==row['digest'],'invalid_archive','Invalid current artifact')
            count=self.db.execute("SELECT count(*) FROM rows WHERE section='revisions' AND ref=?",(row['id'],)).fetchone()[0]
            need(count==row['revision'],'invalid_archive','Historical revision is missing')
            current=self.get('revisions',canonical([row['id'],row['revision']]).decode())
            need(current['digest']==row['digest'],'invalid_archive','Current body differs from final revision')
            if row['status']=='accepted':accepted_count+=1
        seen=set()
        for ref in baseline['artifacts']:
            need(ref['id'] not in seen,'invalid_archive','Duplicate baseline artifact');seen.add(ref['id'])
            current=self.get('artifacts',ref['id'])
            need(current['status']=='accepted' and ref=={k:current[k] for k in ('id','kind','revision','digest')},'invalid_archive','Baseline accepted set differs')
        need(accepted_count==len(seen),'invalid_archive','Accepted requirement/artifact omitted from baseline')
        def checked_revisions():
            for row in self.each('revisions'):
                art=self.get('artifacts',row['artifact'])
                need(type(row['revision']) is int and 1<=row['revision']<=art['revision'] and digest(row['body'])==row['digest'],
                     'invalid_archive','Invalid historical revision')
                yield row

        source_projection=historical_source_reference_projection(
            checked_revisions(), lambda artifact:self.get('artifacts',artifact)['revision'],
            lambda source:self.exists('sources',source), error_code='invalid_archive')
        self.history_projection=source_projection
        previous={}
        for row in self.each('dispositions'):
            source=self.get('sources',row['source']);start,end=row['start'],row['end']
            need(type(start) is int and type(end) is int and 0<=start<end<=source['characters'] and start>=previous.get(row['source'],0),
                 'invalid_archive','Source classification overlaps or lies outside source')
            previous[row['source']]=end
            for ref in row['refs']:self.exists('artifacts',ref)
        for row in self.each('links'):
            self.exists('artifacts',row['source']);self.exists('artifacts',row['target'])
        for row in self.each('decisions'):
            need(digest(row['body'])==row['digest'],'invalid_archive','Decision content changed')
            if row.get('source'):self.exists('sources',row['source'])
        for row in self.each('attempts'):self.exists('changes',row['change_id'])
        for row in self.each('conflicts'):
            if row.get('decision'):self.exists('decisions',row['decision'])
        for row in self.each('programs'):self.exists('sources',row['body']['source'])
        if 'program_origins' in self.counts:
            validate_origin_rows(
                list(self.each('programs')), list(self.each('program_origins')), project,
                code='invalid_archive',
            )
        for row in self.each('packets'):
            need(digest(row['body'])==row['digest'],'invalid_archive','Review packet changed');self.exists('programs',row['body']['program'])
        for row in self.each('breakdowns'):
            self.exists('programs',row['program'])
            need(digest(row['body'])==row['digest'] and row['body']['program']==row['program'],'invalid_archive','Breakdown changed')
            if row['previous']:need(self.get('breakdowns',row['previous'])['program']==row['program'],'invalid_archive','Previous plan belongs elsewhere')
            groups={};ordinal=0
            for member in self.each('members',row['id']):
                need(member['ordinal']==ordinal,'invalid_archive','Packet membership order has holes or duplicates');ordinal+=1
                packet=self.get('packets',member['packet'])['body'];unit=packet['unit']
                need(packet['program']==row['program'] and unit in row['body']['material_bindings'],'invalid_archive','Packet belongs to another plan')
                state=groups.setdefault(unit,{'cursor':0,'hash':hashlib.sha256(),'total':packet['total_characters']})
                text=packet['serialized_fragment']
                need(packet['material_digest']==row['body']['material_bindings'][unit] and packet['start']==state['cursor'] and packet['end']==packet['start']+len(text) and packet['total_characters']==state['total'],
                     'invalid_archive','Review fragment ranges/content differ')
                state['cursor']=packet['end'];state['hash'].update(text.encode())
            need(set(groups)==set(row['body']['material_bindings']),'invalid_archive','Missing historical review material')
            for unit,state in groups.items():
                need(state['cursor']==state['total'] and state['hash'].hexdigest()==row['body']['material_bindings'][unit],'invalid_archive','Incomplete review fragments')
        for row in self.each('members'):
            self.exists('breakdowns',row['breakdown']);self.exists('packets',row['packet'])
        for row in self.each('adoptions'):self.exists('breakdowns',row['breakdown'])
        for row in self.each('closures'):
            self.exists('programs',row['program'])
            need(row['body'].get('delivery')==row['delivery'],'invalid_archive','Closure delivery differs')
        for row in self.each('review_scopes'):
            self.exists('programs',row['program']);need(digest(row['body'])==row['digest'],'invalid_archive','Review scope changed')
        for row in self.each('uploads'):
            self.exists('programs',row['program'])
            if row['result']:self.exists('breakdowns',row['result']['id'])
        for row in self.each('upload_units'):
            self.exists('uploads',row['upload']);need(row['unit']==row['body']['id'],'invalid_archive','Staged unit identity differs')
        for row in self.each('upload_batches'):self.exists('uploads',row['upload'])
        if COLLECTOR_FAILURE_SECTIONS[0] in self.counts:
            for row in self.each('collector_failure_history'):
                need(row.get('project')==project,'invalid_archive','Recovery history crosses project boundary')
                summary=row.get('recovery_artifacts')
                if isinstance(summary,dict) and summary.get('manifest_blob'):
                    raws=self.recovery_raw.get(row['id'],{})
                    manifest_blob=summary['manifest_blob']
                    need(manifest_blob in raws,'invalid_archive','Recovery manifest is not in portable raw closure')
                    manifest=parse_json(self.raw_bytes(raws[manifest_blob]),limit=MAX_MANIFEST_BYTES)
                    need(manifest.get('format')=='failed-artifacts.v1','invalid_archive','Recovery manifest format differs')
                    pages=list(manifest.get('entry_pages',[])) if isinstance(manifest.get('entry_pages'),list) else []
                    index=manifest.get('entry_page_index') or {}
                    index_blob=index.get('blob');index_seen=set()
                    while index_blob:
                        need(index_blob not in index_seen,'invalid_archive','Recovery entry index contains a cycle')
                        index_seen.add(index_blob)
                        need(index_blob in raws,'invalid_archive','Recovery entry index is missing from portable raw closure')
                        index_body=parse_json(self.raw_bytes(raws[index_blob]),limit=4*1024*1024)
                        need(index_body.get('format')=='failed-artifact-entry-index.v1','invalid_archive','Recovery entry index format differs')
                        pages.extend(index_body.get('pages',[]));index_blob=index_body.get('previous')
                    for page in pages:
                        page_blob=page.get('blob');need(page_blob in raws,'invalid_archive','Recovery page is missing from portable raw closure')
                        page_body=parse_json(self.raw_bytes(raws[page_blob]),limit=4*1024*1024)
                        for chunk in page_body.get('chunks',[]):
                            chunk_blob=chunk.get('blob');need(chunk_blob in raws,'invalid_archive','Recovery chunk is missing from portable raw closure')
                            chunk_body=parse_json(self.raw_bytes(raws[chunk_blob]),limit=16*1024*1024)
                            for entry in chunk_body.get('entries',[]):
                                if entry.get('blob'):
                                    need(entry['blob'] in raws,'invalid_archive','Stored recovery artifact is missing from portable raw closure')
        from .task_revisions import validate_history_record, validate_proposal_record
        last_revision = {}
        for record in self.each('task_revision_history'):
            validate_history_record(record, project)
            previous = last_revision.get(record['task'])
            need(previous is None or previous==record['from_revision'],
                 'invalid_archive','Task definition history has a missing revision')
            last_revision[record['task']] = record['to_revision']
            proposal = record['body'].get('proposal')
            if proposal:
                proposed = self.get('task_revision_proposals',proposal)
                need(proposed['task']==record['task'] and proposed['result']
                     and proposed['result']['history']==record['id'],
                     'invalid_archive','Applied proposal and history are not mutually linked')
        for record in self.each('task_revision_proposals'):
            validate_proposal_record(record, project)
            if record['status']=='applied':
                history=self.get('task_revision_history',record['result']['history'])
                need(history['body']['proposal']==record['id']
                     and history['task']==record['task']
                     and history['body']['review_receipt']==record['result']['review_receipt'],
                     'invalid_archive','Applied proposal has inconsistent history')
        features=self.m.get('features') or {}
        if self.m['format'] in {WORKSTREAM_FORMAT,RETURN_FORMAT,SUBPLAN_FORMAT,LOCAL_EXECUTION_FORMAT} or features.get('workstreams'):
            from .workstream_history import validate_history
            validate_history(self.get,self.each,project)
        if self.m['format'] in {RETURN_FORMAT,SUBPLAN_FORMAT,LOCAL_EXECUTION_FORMAT} or features.get('returns'):
            from .scope_return_history import validate_returns
            validate_returns(self.get,self.each,project)
        if self.m['format'] in {SUBPLAN_FORMAT,LOCAL_EXECUTION_FORMAT} or features.get('subplans'):
            from .subplan_history import validate_subplans
            validate_subplans(self.get,self.each,project)
        if self.m['format']==LOCAL_EXECUTION_FORMAT or features.get('local_executions'):
            from .local_execution_history import validate_local_executions
            validate_local_executions(self.get,self.each,project)
        if self.m['format'] in {EXECUTION_CONTROL_FORMAT,COLLECTOR_FAILURE_FORMAT} or features.get('execution_controls'):
            from .execution_control_history import validate_execution_controls
            validate_execution_controls(self.get,self.each,project)
        if self.m['format'] in {TRACEABILITY_FORMAT, ASSURANCE_FORMAT, ORIGIN_FORMAT, DOMAIN_FORMAT} and ((self.m.get('features') or {}).get('traceability') or self.m['format']==TRACEABILITY_FORMAT):
            # Reuse the traceability module's strict table/FK/digest validator
            # for the chunked representation.  The raw_objects checks in
            # load() above separately prove the CAS closure, including failed
            # staging and historical Git pins.
            from .traceability import _validate_archive_rows
            tables={section:list(self.each(section)) for section in TRACEABILITY_SECTIONS}
            context={section:list(self.each(section)) for section in TRACEABILITY_CONTEXT_SECTIONS}
            # Base artifact/revision history is always present in the
            # standard archive and supplies the immutable artifact_ac side of
            # a Unit B edge.
            context['artifacts']=list(self.each('artifacts'))
            context['revisions']=list(self.each('revisions'))
            # Source spans carry a registered source identity in addition to
            # their CAS blob.  Keep the same source rows available to the
            # shared Unit B archive validator as the dedicated export does.
            context['sources']=list(self.each('sources'))
            # Candidate provenance also needs the retained implementation
            # run, observed receipt, and repository identity.  These are
            # historical rows only; inspection never replays or re-signs
            # them.
            context['runs']=list(self.each('runs'))
            context['receipts']=list(self.each('receipts'))
            context['repos']=list(self.each('repos'))
            # A candidate may be a valid historical output of a prior Task
            # definition after the live Task has been replanned.  Preserve
            # the immutable before/after history in the shared context so
            # the validator can resolve the exact candidate->revision edge;
            # a larger current revision is not a substitute for that proof.
            context['task_revision_history']=list(self.each('task_revision_history'))
            project_record=self.get('project_record',project)
            _validate_archive_rows({'project':project,'project_record':project_record,'tables':tables,
                                    'context':context}, self.trace_blob_bytes.get)
        if self.m['format'] in {ASSURANCE_FORMAT, ORIGIN_FORMAT, DOMAIN_FORMAT}:
            from .assurance import validate_assurance_rows
            tables={section:list(self.each(section)) for section in ASSURANCE_SECTIONS}
            # The assurance validator must resolve candidate execution
            # provenance through the same finite PinnedContext as live reads.
            # Expose the already archived context projection on the read-only
            # endpoint callback; a callback that can fetch one row by ID is
            # deliberately insufficient to certify Task history/run/receipt
            # and repository/CAS closure.
            assurance_context = {
                section: list(self.each(section))
                for section in TRACEABILITY_CONTEXT_SECTIONS
            }
            # Consumer-P artifact-production material points at draft or
            # accepted Knowledge artifacts and immutable revisions.  Keep
            # those rows in the same read-only archive context; the resolver
            # never falls back to the live database.
            assurance_context['artifacts'] = list(self.each('artifacts'))
            assurance_context['sources'] = list(self.each('sources'))
            assurance_context['traceability_items'] = list(self.each('traceability_items'))
            assurance_context['revisions'] = list(self.each('revisions'))
            assurance_context['task_revision_history'] = list(
                self.each('task_revision_history'))
            def archived_external(section, key):
                try:
                    return self.get(section, key)
                except Fault:
                    return None
            archived_external.context_rows = assurance_context
            def archived_assurance_blob(ident):
                raw = self.trace_blob_bytes.get(ident)
                if raw is None and ident in self.source_raw:
                    descriptor = self.source_raw[ident]
                    need(descriptor['bytes'] <= MAX_RECORD_BYTES, 'archive_record_too_large', 'DOMAIN source exceeds bounded semantic read')
                    raw = b''.join(checked_chunks(self.m, descriptor['chunks'], self.read, self.used))
                need(isinstance(raw, bytes), 'invalid_archive',
                     'Assurance material CAS child is missing from the archive', ident)
                need(digest(raw) == ident, 'invalid_archive',
                     'Assurance material CAS child digest differs', ident)
                return raw
            validate_assurance_rows(tables, project, archived_external, archived_assurance_blob)
            for row in tables['assurance_objects']:
                body=row.get('body'); body=parse_json(body,limit=1024*1024) if isinstance(body,str) else body
                payload_blob=body.get('payload_blob') if isinstance(body,dict) else None
                if isinstance(payload_blob,str):
                    from .assurance import _material_child_digests
                    raws=self.assurance_raw.get(row['id'], set())
                    pending=[payload_blob];seen=set()
                    while pending:
                        child=pending.pop()
                        if child in seen:continue
                        seen.add(child)
                        need(child in raws,'invalid_archive','Assurance material CAS child is missing from portable CAS closure',child)
                        raw=self.trace_blob_bytes.get(child)
                        need(isinstance(raw,bytes),'invalid_archive','Assurance material CAS child bytes are missing',child)
                        need(digest(raw)==child,'invalid_archive','Assurance material CAS child digest differs',child)
                        try:parsed=parse_json(raw,limit=1024*1024)
                        except Fault:continue
                        for _path,nested in _material_child_digests(parsed):
                            pending.append(nested)
            # Only actual CAS leaves require raw archive material; ordinary
            # identity digests remain historical references.
            for row in tables['assurance_refs']:
                ref=row['ref_digest']
                key=canonical([row['object_id'],row['ordinal']]).decode()
                raws=self.assurance_raw.get(key, set())
                need(all(isinstance(raw,str) and len(raw)==64 for raw in raws),
                     'invalid_archive','Assurance raw closure contains a malformed digest')
                # ``assurance_raw`` stores the referenced digest set, while
                # ``trace_blob_bytes`` retains the verified raw bytes from the
                # descriptor.  Validate the bytes without treating the digest
                # string itself as a descriptor.
                need(ref not in raws or (ref in self.trace_blob_bytes and
                                         digest(self.trace_blob_bytes[ref])==ref),
                     'invalid_archive','Assurance raw closure digest differs')
        return {'subplans':self.counts['subplans'],'subplan_packets':self.counts['subplan_packets'],'subplan_compositions':self.counts['subplan_compositions'],'scope_returns':self.counts['scope_returns'],'scope_return_packets':self.counts['scope_return_packets'],'workstreams':self.counts['workstreams'],'workstream_packets':self.counts['workstream_packets'],'workstream_records':self.counts['workstream_records'], 'local_execution_proposals':self.counts['local_execution_proposals'],'local_execution_packets':self.counts['local_execution_packets'],'local_execution_records':self.counts['local_execution_records'], 'artifacts':self.counts['artifacts'],'revisions':self.counts['revisions'],'sources':self.counts['sources'],
                'classifications':self.counts['dispositions'],'links':self.counts['links'],'documents':self.counts['documents'],
                'decisions':self.counts['decisions'],'changes':self.counts['changes'],'programs':self.counts['programs'],
                'program_origins':self.counts.get('program_origins',0),
                'breakdowns':self.counts['breakdowns'],'packets':self.counts['packets'],'uploads':self.counts['uploads'],
                'upload_units':self.counts['upload_units'],'upload_batches':self.counts['upload_batches'],
                'task_revision_proposals':self.counts['task_revision_proposals'],'task_revision_history':self.counts['task_revision_history'],
                'assurance_objects':self.counts.get('assurance_objects',0),'assurance_events':self.counts.get('assurance_events',0),
                'assurance_heads':self.counts.get('assurance_heads',0),'assurance_refs':self.counts.get('assurance_refs',0),
                'tasks':self.counts['tasks'],'execution_attempts':self.counts['execution_attempts'],
                'attempt_assessments':self.counts['attempt_assessments'],
                'execution_control_proposals':self.counts['execution_control_proposals'],
                'execution_control_packets':self.counts['execution_control_packets'],
                'execution_control_events':self.counts['execution_control_events'],
                'execution_control_authorizations':self.counts['execution_control_authorizations'],
                'collector_failure_history':self.counts['collector_failure_history'],
                'traceability_sets':self.counts['traceability_sets'],
                'traceability_revisions':self.counts['traceability_revisions'],
                'traceability_items':self.counts['traceability_items'],
                'traceability_proposals':self.counts['traceability_proposals'],
                'traceability_decisions':self.counts['traceability_decisions'],
                'traceability_mappings':self.counts['traceability_mappings'],
                'traceability_bindings':self.counts['traceability_bindings'],
                'traceability_records':self.counts['traceability_records']}

    def raw_bytes(self, data):
        """Read one bounded metadata object for transitive-closure checks."""
        need(type(data.get('bytes')) is int and data['bytes'] <= MAX_RECORD_BYTES,
             'invalid_archive','Recovery metadata object exceeds the explicit inspection limit')
        value=bytearray()
        for block in checked_chunks(self.m,data['chunks'],self.read,self.used):
            value.extend(block)
            need(len(value)<=MAX_RECORD_BYTES,'invalid_archive','Recovery metadata object exceeds the explicit inspection limit')
        need(len(value)==data['bytes'] and digest(bytes(value))==data['sha256'],
             'invalid_archive','Recovery metadata object changed')
        return bytes(value)


def validate(manifest,read,*,include_projection=False):
    """No original store, Git, Agent, key, or provider is necessary."""
    try:
        need(manifest['format'] in FORMATS and set(manifest['records']['counts'])==set(sections_for(manifest)),'invalid_archive','Unsupported archive format/sections')
        need(manifest.get('runtime_restore_supported') is False and manifest.get('fresh_test_or_review_evidence') is False,
             'invalid_archive','Historical archive cannot assert new execution/acceptance')
        need(len(canonical(manifest))<=MAX_MANIFEST_BYTES,'invalid_archive','Manifest exceeds limit')
        need(sum(part['bytes'] for part in manifest['objects'].values())<=MAX_ARCHIVE_BYTES,'invalid_archive','Archive exceeds disk bound')
        for h,part in manifest['objects'].items():
            need(len(h)==64 and all(ch in '0123456789abcdef' for ch in h) and type(part['bytes']) is int and 0<part['bytes']<=MAX_CHUNK_BYTES,'invalid_archive','Invalid chunk descriptor')
        with tempfile.TemporaryDirectory(prefix='daikibo-inspect-') as tmp:
            db=sqlite3.connect(Path(tmp)/'index.sqlite3')
            try:
                inspector=Inspector(db,manifest,read)
                counts=inspector.validate()
                if include_projection:
                    return {'counts':dict(counts),**inspector.history_projection}
                return counts
            finally:db.close()
    except (KeyError,TypeError,ValueError,UnicodeError,sqlite3.IntegrityError,zipfile.BadZipFile) as exc:
        raise Fault('invalid_archive','Malformed, incomplete or duplicated knowledge archive',str(exc)) from exc


def snapshot_files(manifest,root_blob,store):
    files={'baseline.json':{'kind':'file','blob':root_blob,'size':len(canonical(manifest)),'mode':0o100644}}
    for h,info in manifest['objects'].items():files['objects/'+h]={'kind':'file','blob':h,'size':info['bytes'],'mode':0o100644}
    return files


def export_zip(store,manifest,root_blob,destination):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.chunk-export-',dir=destination.parent)
    header={'format':archive_format(manifest),'baseline':manifest['baseline_id'],'project':manifest['project'],
            'snapshot':{'sha256':root_blob,'bytes':len(canonical(manifest))},'runtime_restore_supported':False}
    try:
        with os.fdopen(fd,'wb') as stream:
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
                for name,data in [('manifest.json',canonical(header)),('snapshot.json',canonical(manifest))]:
                    info=zipfile.ZipInfo(name,(2026,9,11,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,data)
                for h in sorted(manifest['objects']):
                    info=zipfile.ZipInfo('objects/'+h,(2026,9,11,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                    # No aggregate archive/read_bytes copy, including files >256MiB.
                    with archive.open(info,'w',force_zip64=True) as target:
                        hashed=hashlib.sha256();total=0;offset=0
                        with open_artifact_session(store,h) as session:
                            size=session.size
                            while offset < size:
                                block=session.read_range(offset,min(CHUNK_BYTES,size-offset))
                                need(block,'integrity_error','Chunk artifact ended while exporting')
                                hashed.update(block);total+=len(block);offset+=len(block);target.write(block)
                        need(hashed.hexdigest()==h and total==manifest['objects'][h]['bytes'],'integrity_error','Chunk changed while exporting')
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,destination)
    finally:Path(temporary).unlink(missing_ok=True)
    return file_digest(destination)


def inspect_zip(source,expected_sha256):
    source=Path(source)
    need(source.is_file() and source.stat().st_size<=MAX_ARCHIVE_BYTES,'invalid_archive','Archive exceeds explicit disk bound')
    need(file_digest(source)==expected_sha256,'archive_mismatch','Archive checksum differs')
    try:
        with zipfile.ZipFile(source) as archive:
            names=archive.namelist();need(len(names)==len(set(names)),'invalid_archive','Duplicate ZIP members')
            need(archive.getinfo('manifest.json').file_size<=65536 and archive.getinfo('snapshot.json').file_size<=MAX_MANIFEST_BYTES,'invalid_archive','Metadata exceeds limit')
            header=parse_json(archive.read('manifest.json'),limit=65536);raw=archive.read('snapshot.json')
            need(header['format'] in ARCHIVE_FORMATS and header.get('runtime_restore_supported') is False and len(raw)==header['snapshot']['bytes'] and digest(raw)==header['snapshot']['sha256'],'invalid_archive','Archive header differs')
            manifest=parse_json(raw,limit=MAX_MANIFEST_BYTES)
            need(header['format']==archive_format(manifest) and manifest['baseline_id']==header['baseline'] and manifest['project']==header['project'],'invalid_archive','Archive identity differs')
            expected={'manifest.json','snapshot.json'}|{'objects/'+h for h in manifest['objects']}
            need(set(names)==expected,'invalid_archive','Missing or unexpected chunk files')
            def read(h):
                name='objects/'+h
                need(archive.getinfo(name).file_size==manifest['objects'][h]['bytes']<=MAX_CHUNK_BYTES,'invalid_archive','Chunk size differs')
                return archive.read(name)
            inspection=validate(manifest,read,include_projection=True)
            counts=inspection['counts']
            report={'verified':True,'baseline':header['baseline'],'project':header['project'],'snapshot_digest':header['snapshot']['sha256'],
                    'format':header['format'],'counts':counts,'runtime_restore_supported':False,'new_test_or_review_evidence':False}
            report.update({key:inspection[key] for key in (
                'history_integrity','historical_reference_diagnostic_count',
                'historical_reference_diagnostics','historical_reference_diagnostics_total',
                'historical_reference_diagnostics_truncated','historical_reference_interpretation')
                if key in inspection})
            return report
    except (KeyError,TypeError,ValueError,zipfile.BadZipFile) as exc:
        raise Fault('invalid_archive','Malformed chunk archive') from exc
