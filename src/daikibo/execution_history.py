"""Read retained run bytes without adopting them or bypassing task gates.

``work_*`` reads the ordinary run receipt's working-product snapshot.  The
``recovery_*`` methods are a separate read family for a failure-retention
manifest.  Both families are bounded and digest-bound; neither changes task,
candidate, receipt, or success state.  Callers must keep the selected run,
entry, digest, and page together and treat base64 as transport only.
"""
from __future__ import annotations

import base64

from .common import digest,need,number,parse_json


class ExecutionHistory:
    def __init__(self,store,knowledge,governance,retention=None):
        self.s,self.k,self.g,self.retention=store,knowledge,governance,retention

    def recovery(self, actor, run, offset=0, limit=100, expected_digest=None):
        """Read one bounded failed-artifact manifest page under the run scope."""
        need(self.retention is not None, 'recovery_unavailable', 'Failure-retention reader is unavailable')
        return self.retention.detail(actor,run,offset,limit,expected_digest)

    def recovery_read(self, actor, run, repo, path, expected_digest, offset=0, limit=65536, expected_manifest=None):
        """Stream one retained failed file by manifest and file digest."""
        need(self.retention is not None, 'recovery_unavailable', 'Failure-retention reader is unavailable')
        return self.retention.read(actor,run,repo,path,expected_digest,offset,limit,expected_manifest)

    def _load(self,actor,run):
        row=self.s.one('SELECT project FROM runs WHERE id=?',(run,),True);self.k.project(actor,row['project'])
        record=self.s.one('SELECT id FROM receipts WHERE run=?',(run,),True)
        receipt=self.g.receipt(record['id'])
        product=receipt.get('work_product') or receipt.get('partial_work')
        need(product,'no_work_product','No retained working files were observed for this run')
        snapshot=parse_json(self.s.blob_get(product['snapshot_blob']),limit=256*1024*1024)
        unsigned={k:v for k,v in snapshot.items() if k!='digest'}
        need(digest(unsigned)==snapshot['digest'],'integrity_error','Retained snapshot digest differs')
        candidate=self.s.one('SELECT id FROM candidates WHERE implementation_run=?',(run,))
        return receipt,product,snapshot,candidate

    def changes(self,actor,run,offset=0,limit=100):
        """List ordinary working-product changes; the result is never adoptable."""
        number(offset,'offset',0,10**12,integer=True);number(limit,'limit',1,1000,integer=True)
        receipt,product,snapshot,candidate=self._load(actor,run)
        changes=parse_json(self.s.blob_get(product['changes_blob']),limit=256*1024*1024)
        return {'run':run,'receipt':receipt['id'],'snapshot_blob':product['snapshot_blob'],'changes_blob':product['changes_blob'],
                'snapshot_digest':snapshot['digest'],'changes':changes[offset:offset+limit],
                'next_offset':offset+limit if offset+limit<len(changes) else None,'total_changes':len(changes),
                'candidate':candidate['id'] if candidate else None,'accepted_completion':False,
                'notice':'Inspection does not adopt files, rerun tests, approve a candidate or complete a task.'}

    def read_file(self,actor,run,repo,path,expected_digest,offset=0,limit=65536):
        """Read a digest-bound regular working-product file page."""
        number(offset,'offset',0,10**12,integer=True);number(limit,'limit',1,1048576,integer=True)
        receipt,product,snapshot,candidate=self._load(actor,run)
        need(repo in snapshot['repos'] and path in snapshot['repos'][repo]['files'],'not_found','File is not in the recorded working product')
        entry=snapshot['repos'][repo]['files'][path]
        need(entry['kind']=='file','not_regular_file','A symbolic link is not dereferenced as recorded file data')
        need(entry['blob']==expected_digest,'stale_work_product','Expected file digest differs')
        content=self.s.blob_get(entry['blob']);end=min(len(content),offset+limit)
        return {'run':run,'receipt':receipt['id'],'repo':repo,'path':path,'sha256':entry['blob'],
                'base64':base64.b64encode(content[offset:end]).decode('ascii'),'total_bytes':len(content),
                'next_offset':end if end<len(content) else None,'adopted_by_read':False}
