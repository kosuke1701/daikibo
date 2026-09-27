"""Lossless bounded document intake. Unknown bytes stay unknown, never guessed away."""
from __future__ import annotations
import base64,binascii
from .common import Fault,canonical,digest,need,obj,parse_json,text,timestamp,uid

class Documents:
    def __init__(self,c):self.c=c;self.s=c.s
    def register(self,actor,project,content_base64,locator,media_type='application/octet-stream'):
        actor.require('owner','agent',project=project);self.c.k.project(actor,project);text(locator,'locator',4096);text(media_type,'media type',200)
        need(isinstance(content_base64,str) and len(content_base64)<=7_000_000,'invalid_document','Document exceeds bounded upload size')
        try:data=base64.b64decode(content_base64,validate=True)
        except (binascii.Error,ValueError):need(False,'invalid_document','Invalid base64')
        h=self.s.blob_put(data);ident=uid('DOC');source=None;unknown=[]
        normalized_media=media_type.split(';',1)[0].strip().lower()
        json_media=normalized_media=='application/json' or normalized_media.endswith('+json')
        textual=normalized_media.startswith('text/') or json_media or normalized_media in {'application/yaml','application/x-yaml','application/toml'}
        if textual:
            try:
                value=data.decode('utf-8-sig')
                if json_media:parse_json(value,limit=6_000_000)
                if value:source=self.c.k.source(actor,project,value,locator)
                else:unknown.append('empty_text')
            except (UnicodeDecodeError,Fault) as exc:
                # Byte-exact original was stored first. Bad encoding/invalid JSON isn't dropped.
                unknown.append('text_decode_or_format_error:'+type(exc).__name__)
        else:unknown.append('unsupported_format_requires_reviewed_extraction')
        body={'id':ident,'project':project,'raw_digest':h,'bytes':len(data),'locator':locator,'media_type':media_type,'text_source':source,'unknown':unknown,'raw_trust':'human' if actor.role=='owner' else 'agent'}
        with self.s.transaction():
            self.s.execute('INSERT INTO documents VALUES(?,?,?,?,?)',(ident,project,canonical(body).decode(),'needs_extraction' if unknown else 'text_ready',timestamp()))
            if unknown:self.c.g.inbox(project,'unknown_document',ident,body,'warning')
            self.c.sec.event(project,'document_imported',actor.id,body)
        return body
    def get(self,actor,document):
        row=self.s.one('SELECT * FROM documents WHERE id=?',(document,),True);self.c.k.project(actor,row['project']);row['body']=parse_json(row['body']);return row
    def attach_text(self,actor,document,raw_digest,content,reason):
        # Owner approves a lossy/external extraction; agents can propose sources, not certify the conversion.
        actor.require('owner');row=self.get(actor,document);body=row['body'];need(body['raw_digest']==raw_digest,'stale_document','Wrong original bytes');text(reason,'extraction provenance',10000)
        source=self.c.k.source(actor,row['project'],content,body['locator']+'#human-verified-extraction')
        body['extractions']=body.get('extractions',[])+[{'source':source,'reason':reason,'verified_by':actor.id,'raw_digest':raw_digest}]
        with self.s.transaction():
            self.s.execute("UPDATE documents SET body=?,status='extraction_verified' WHERE id=?",(canonical(body).decode(),document))
            self.s.execute("UPDATE inbox SET status='resolved' WHERE kind='unknown_document' AND ref=?",(document,))
            self.c.sec.event(row['project'],'document_extraction_adopted',actor.id,{'document':document,'source':source,'reason':reason})
        return source
