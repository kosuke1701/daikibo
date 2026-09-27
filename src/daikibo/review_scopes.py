"""Bounded independent review packets; full phase coverage is aggregated mechanically."""
from __future__ import annotations
from .common import canonical,digest,need,parse_json,timestamp,uid

class ReviewScopes:
    def __init__(self,c):self.c=c;self.s=c.s
    def source_item(self, source, include_content=True):
        row=self.s.one('SELECT id,blob,characters,trust FROM sources WHERE id=?',(source,),True)
        spans=self.s.all('SELECT start,end,category,refs,reason FROM dispositions WHERE source=? ORDER BY start,end',(source,))
        for span in spans:span['refs']=parse_json(span['refs'])
        binding=digest({'source_digest':row['blob'],'dispositions':spans})
        item={'type':'source','id':source,'digest':binding,'source_digest':row['blob'],'characters':row['characters'],'trust':row['trust']}
        if include_content:item['body']={'content':self.s.blob_get(row['blob']).decode(),'dispositions':spans}
        return item

    def partition(self,actor,program,byte_budget=100000):
        row=self.s.one('SELECT * FROM programs WHERE id=?',(program,),True);actor.require('owner','agent',project=row['project'])
        need(type(byte_budget) is int and 10000<=byte_budget<=400000,'invalid_budget','Review packet budget must be 10k..400k UTF-8 bytes')
        project=row['project'];items=[]
        for source in self.s.all('SELECT id FROM sources WHERE project=? ORDER BY id',(project,)):
            items.append(self.source_item(source['id']))
        for art in self.s.all('SELECT id,revision,digest,kind,owner,body,status FROM artifacts WHERE project=? ORDER BY owner,kind,id',(project,)):
            items.append({'type':'artifact',**art,'body':parse_json(art['body'])})
        if row['phase'] in {'plan','implementation','integration','delivery'}:
            for task in self.s.all("SELECT id,revision,body,status,validity FROM tasks WHERE project=? AND status!='cancelled' ORDER BY id",(project,)):
                task['body']=parse_json(task['body']);task['binding']=self.c.g.task_binding(task['id']);items.append({'type':'task',**task})
        need(items,'empty_review_scope','No specification artifacts to review')
        from .packets import slices
        expanded=[]
        for item in items:
            if len(canonical(item)) < byte_budget-2000:
                expanded.append(item); continue
            serialized=canonical(item['body']).decode()
            parts=list(slices(serialized, max(256,(byte_budget-2500)//2)))
            for ordinal,(start,end,value) in enumerate(parts):
                fragment={k:v for k,v in item.items() if k!='body'}
                fragment['body']={'serialized_fragment':value}
                fragment['fragment']={'index':ordinal,'count':len(parts),'start':start,'end':end,
                                      'full_body_digest':digest(item['body']),'encoding':'canonical-json-utf8'}
                need(len(canonical(fragment)) < byte_budget-1500,'context_insufficient','Fragment metadata exceeds budget')
                expanded.append(fragment)
        packets=[];current=[]
        for item in expanded:
            if current and len(canonical(current+[item]))>byte_budget-2000:packets.append(current);current=[]
            current.append(item)
        if current:packets.append(current)
        result=[]
        expected_keys=[(i['type'],i['id'],i.get('fragment',{}).get('index',-1)) for i in expanded]
        with self.s.transaction():
            self.s.execute("UPDATE review_scopes SET status='superseded' WHERE program=? AND phase=?",(program,row['phase']))
            for packet in packets:
                ident=uid('RSCOPE');body={'program':program,'phase':row['phase'],'items':packet,'policy':self.c.g.policy(project)['digest'],'authority':'Review these complete artifacts. Names of linked external artifacts are not proof of their content; request a bounded follow-up when necessary.'}
                self.s.execute('INSERT INTO review_scopes VALUES(?,?,?,?,?,?,?,?)',(ident,program,project,row['phase'],canonical(body).decode(),digest(body),'active',timestamp()))
                result.append({'id':ident,'digest':digest(body),'items':len(packet),'bytes':len(canonical(body))})
            manifest={'expected_keys':expected_keys,'scopes':[p['id'] for p in result]}
            self.s.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('review-manifest:'+program+':'+row['phase'],canonical(manifest).decode()))
            self.c.sec.event(project,'phase_review_partitioned',actor.id,{'program':program,'phase':row['phase'],'packets':result,'items':len(items)})
        return {'program':program,'phase':row['phase'],'packets':result,'total_items':len(items),'scope_not_reduced':True}
    def current(self,actor,scope):
        row=self.s.one('SELECT * FROM review_scopes WHERE id=?',(scope,),True);self.c.k.project(actor,row['project']);body=parse_json(row['body'])
        program=self.s.one('SELECT phase FROM programs WHERE id=?',(row['program'],),True)
        need(row['status']=='active' and program['phase']==row['phase'],'stale_review_scope','Review scope is no longer active')
        need(body['policy']==self.c.g.policy(row['project'])['digest'],'stale_review_scope','Policy changed')
        for item in body['items']:
            if item['type']=='artifact':
                current=self.c.k.artifact(actor,item['id']);need((current['revision'],current['digest'],current['status'])==(item['revision'],item['digest'],item['status']),'stale_review_scope','Artifact changed',item['id'])
            elif item['type']=='source':
                need(self.source_item(item['id'],include_content=False)['digest']==item['digest'],
                     'stale_review_scope','Source classification changed',item['id'])
            else:need(self.c.g.task_binding(item['id'])==item['binding'],'stale_review_scope','Task changed',item['id'])
        row['body']=body;return row
    def summary(self,actor,program):
        row=self.s.one('SELECT * FROM programs WHERE id=?',(program,),True);self.c.k.project(actor,row['project'])
        scopes=self.s.all("SELECT id,digest FROM review_scopes WHERE program=? AND phase=? AND status='active' ORDER BY id",(program,row['phase']))
        expected={('artifact',r['id']) for r in self.s.all('SELECT id FROM artifacts WHERE project=?',(row['project'],))}
        expected|={('source',r['id']) for r in self.s.all('SELECT id FROM sources WHERE project=?',(row['project'],))}
        if row['phase'] in {'plan','implementation','integration','delivery'}:expected|={('task',r['id']) for r in self.s.all("SELECT id FROM tasks WHERE project=? AND status!='cancelled'",(row['project'],))}
        covered=set();covered_parts=set();packets=[];failures=[]
        manifest_row=self.s.one('SELECT value FROM meta WHERE key=?',('review-manifest:'+program+':'+row['phase'],))
        manifest=parse_json(manifest_row['value']) if manifest_row else None
        for scope in scopes:
            try:
                current=self.current(actor,scope['id']);covered|={(i['type'],i['id']) for i in current['body']['items']}
                covered_parts|={(i['type'],i['id'],i.get('fragment',{}).get('index',-1)) for i in current['body']['items']}
                receipts=self.c.g.evidence_for(scope['id'],scope['digest'],'phase')
                need(receipts,'review_required','Review packet has not been executed')
                observed=self.c.g.require_review(receipts[0]['id'],scope['id'],scope['digest'],{'phase'})
                packets.append({'id':scope['id'],'digest':scope['digest'],'receipt':receipts[0]['id'],'items':len(current['body']['items']),
                                'result':observed['result']})
            except Exception as exc:failures.append({'scope':scope['id'],'error':getattr(exc,'code',type(exc).__name__)})
        if manifest:
            missing_parts=set(tuple(k) for k in manifest['expected_keys'])-covered_parts
            if missing_parts:failures.append({'error':'missing_review_fragments','parts':[list(x) for x in sorted(missing_parts)]})
            if set(manifest['scopes'])!={s['id'] for s in scopes}:failures.append({'error':'review_scope_set_changed'})
        need(scopes,'partition_required','No bounded review partition exists')
        return {'program':program,'phase':row['phase'],'packets':packets,'failures':failures,'missing':[list(x) for x in sorted(expected-covered)],'complete':not failures and expected==covered,'semantic_correctness_guaranteed':False}
