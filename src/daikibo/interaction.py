"""D01 — immutable incoming intent and a non-suppressible decision inbox."""
from __future__ import annotations
from .common import Actor, Fault, canonical, digest, need, parse_json, text, timestamp

class Interaction:
    def __init__(self,store,security,knowledge,planning,workflow,governance):
        self.s,self.sec,self.k,self.p,self.w,self.g=store,security,knowledge,planning,workflow,governance;self.nav=None

    @staticmethod
    def _notice_version_digest(row):
        body_digest=digest(row['body'].encode())
        return digest({'body_digest':body_digest,'severity':row['severity'],'due':row['due']})

    def _notice_answer_after_publication(self,row,source,source_created,was_new_source=False):
        published=self.s.one("SELECT seq,body FROM events WHERE project=? AND kind='notification_published' "
                              "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",
                              (row['project'],row['id']))
        if published:
            material=parse_json(published['body'])
            need(material.get('version_digest')==self._notice_version_digest(row),
                 'stale_notice','The current notification version has no matching publication record')
            registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                                  "AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",
                                  (row['project'],source))
            need(registered and registered['seq']>published['seq'],'stale_user_input',
                 'The answer predates the current notification version')
            return {'after':source_created,'published_seq':published['seq'],'source_seq':registered['seq']}
        # Existing databases may contain notices created before publication
        # events were introduced. Their last-known version is bounded by the
        # original timestamp until the notice next changes.
        if was_new_source:
            return {'after':source_created,'published_seq':None,'source_seq':None}
        need(source_created>=row['created'],'stale_user_input',
             'The answer predates the current notification version')
        return {'after':row['created'],'published_seq':None,'source_seq':None}

    def intake(self,actor,content,project=None,name='New project',bounded=False,start_program=False):
        actor.require('owner','agent',project=project)
        need(type(bounded) is bool,'invalid_option','bounded must be boolean')
        need(type(start_program) is bool,'invalid_option','start_program must be boolean')
        need(not bounded or self.nav is not None,'navigation_unavailable','Bounded notification service is not connected')
        if project is None:project=self.k.create_project(actor,name)['id']
        source=self.k.source(actor,project,content,'conversation')
        warnings=self.nav.inbox(actor,project,limit=30) if bounded else self.inbox(actor,project)
        existing=self.s.one("SELECT id FROM programs WHERE project=? ORDER BY created DESC,id LIMIT 1",(project,))
        # Bootstrap once. Conversation at delivery/closure never starts a new cycle.
        program=self.p.begin(actor,project,source['id'],compact=bounded) if start_program or not existing else {'id':existing['id']}
        return {'project':project,'source':source,'workflow':program,'mandatory_notifications':warnings,
                'human_authority':source['trust']=='human','instruction':'Discuss missing product meaning; keep speculative alternatives as drafts. Use change/reopen for existing work or explicitly begin a new program for new work.'}

    def inbox(self,actor,project):
        self.k.project(actor,project)
        with self.s.transaction():
            rows=self.s.all("SELECT * FROM inbox WHERE project=? AND status='open' ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,created",(project,))
            for row in rows:self.s.execute('UPDATE inbox SET displayed=displayed+1 WHERE id=?',(row['id'],))
            if rows:self.sec.event(project,'inbox_displayed',actor.id,{'ids':[r['id'] for r in rows],'is_acknowledgement':False})
        return [{'id':r['id'],'kind':r['kind'],'ref':r['ref'],'severity':r['severity'],'body':parse_json(r['body']),'due':r['due'],'status':r['status']} for r in rows]

    def acknowledge(self,actor,item,utterance,source=None,expected_digest=None):
        actor.require('owner');text(utterance,'acknowledgement',10000)
        with self.s.transaction():
            row=self.s.one('SELECT * FROM inbox WHERE id=?',(item,),True)
            need(row['kind'] not in {'product_decision','provisional_decision','conflict'},
                 'adjudication_required','Use the bound decision response workflow, not a generic acknowledgement')
            notice_digest=digest(row['body'].encode())
            need(expected_digest is None or expected_digest==notice_digest,'stale_notice','Notification content changed')
            if row['status']=='acknowledged':
                self._exact_acknowledgement_retry(row,utterance,source,notice_digest)
                return {'id':item,'acknowledged':True,'exceptions_still_visible_in_status':True}
            need(row['status']=='open','already_closed','This notification is already closed')
            was_new_source=source is None
            if was_new_source:
                original=self.k.source(actor,row['project'],utterance,'inbox-ack:'+item)
                self.k.classify(actor,original['id'],0,original['characters'],'reference',[],'Acknowledgement, not a new functional requirement')
                source=original['id']
            else:
                original=self.s.one('SELECT created FROM sources WHERE id=?',(source,),True)
            answer_version=self._notice_answer_after_publication(row,source,original['created'],was_new_source)
            if was_new_source:
                quote={'source':original['id'],'source_digest':original['digest'],
                       'start':0,'end':original['characters'],'quote':utterance}
            else:
                quote=self.k.human_quote(actor,row['project'],source,utterance,after=answer_version['after'])
            prior_ack=self.s.one("SELECT seq FROM events WHERE project=? AND kind='inbox_acknowledged' "
                "AND json_extract(body,'$.id')=? AND json_extract(body,'$.notice_version_digest')=? "
                "ORDER BY seq DESC LIMIT 1",(row['project'],item,self._notice_version_digest(row)))
            if prior_ack:
                registered_seq=answer_version['source_seq']
                if registered_seq is None:
                    registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
                        "AND json_extract(body,'$.source')=? ORDER BY seq DESC LIMIT 1",(row['project'],source))
                    registered_seq=registered['seq'] if registered else None
                need(registered_seq is not None and registered_seq>prior_ack['seq'],'stale_user_input',
                     'A reissued notification needs a new acknowledgement source')
            cursor=self.s.execute("UPDATE inbox SET status='acknowledged' WHERE id=? AND status='open'",(item,))
            need(cursor.rowcount==1,'already_closed','This notification was closed while recording the acknowledgement')
            self.sec.event(row['project'],'inbox_acknowledged',actor.id,
                           {'id':item,'notice_digest':notice_digest,
                            'notice_version_digest':self._notice_version_digest(row),
                            'notification_published_seq':answer_version['published_seq'],
                            'source_registered_seq':answer_version['source_seq'],
                            **quote,'waiver_resolved':False})
        return {'id':item,'acknowledged':True,'exceptions_still_visible_in_status':True}

    def _exact_acknowledgement_retry(self,row,utterance,source,notice_digest):
        event=self.s.one("SELECT seq,body FROM events WHERE project=? AND kind='inbox_acknowledged' "
                         "AND json_extract(body,'$.id')=? ORDER BY seq DESC LIMIT 1",
                         (row['project'],row['id']))
        need(event is not None,'already_closed',
             'This notification was closed automatically and has no human acknowledgement to replay')
        answer=parse_json(event['body'])
        version_digest=self._notice_version_digest(row)
        need(answer.get('notice_digest')==notice_digest and answer.get('notice_version_digest')==version_digest,
             'already_closed','The human acknowledgement belongs to an older notification version')
        published=self.s.one("SELECT seq FROM events WHERE project=? AND kind='notification_published' "
            "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",(row['project'],row['id']))
        published_seq=published['seq'] if published else None
        need(answer.get('notification_published_seq')==published_seq,
             'already_closed','The notification was reissued after this acknowledgement')
        later_close=self.s.one("SELECT seq FROM events WHERE project=? AND kind='notification_closed' "
            "AND json_extract(body,'$.item')=? AND seq>? ORDER BY seq DESC LIMIT 1",
            (row['project'],row['id'],event['seq']))
        need(later_close is None,'already_closed',
             'The notification was automatically closed after this human acknowledgement')
        source_id=answer.get('source')
        need(isinstance(source_id,str),'already_closed','The acknowledgement has no retained human source')
        source_row=self.s.one('SELECT project,trust,blob,locator,characters FROM sources WHERE id=?',(source_id,))
        need(source_row is not None and source_row['project']==row['project'] and source_row['trust']=='human' and
             answer.get('source_digest',source_row['blob'])==source_row['blob'],
             'already_closed','The acknowledgement source is no longer the exact trusted source')
        registered=self.s.one("SELECT seq FROM events WHERE project=? AND kind='source_registered' "
            "AND json_extract(body,'$.source')=? AND json_extract(body,'$.digest')=? ORDER BY seq DESC LIMIT 1",
            (row['project'],source_id,source_row['blob']))
        recorded_source_seq=answer.get('source_registered_seq')
        need(registered is not None and registered['seq']<event['seq'] and
             (recorded_source_seq is None or recorded_source_seq==registered['seq']) and
             (published_seq is None or registered['seq']>published_seq),
             'already_closed','The acknowledgement source is not current for this notification')
        content=self.s.blob_get(source_row['blob']).decode('utf-8')
        start,end,quoted=answer.get('start'),answer.get('end'),answer.get('quote')
        need(type(start) is int and type(end) is int and isinstance(quoted,str) and
             0<=start<end<=len(content) and content[start:end]==quoted,
             'already_closed','The acknowledgement quotation does not match its source')
        if source is None:
            same_request=(source_row['locator']=='inbox-ack:'+row['id'] and utterance==quoted and
                          start==0 and end==len(content) and content==quoted)
        else:
            same_request=(source==source_id and utterance==quoted and
                          content.find(utterance)==start and end==start+len(utterance))
        need(same_request,'already_closed',
             'This notification is closed; only its exact human acknowledgement can be replayed')

    def waiver_close(self,actor,waiver):
        row=self.s.one('SELECT * FROM waivers WHERE id=?',(waiver,),True)
        actor.require('owner','agent',project=row['project'])
        body=parse_json(row['body']);task=self.w.task(actor,body['remediation_task'])
        need(task['status']=='completed' and task['validity']=='current','remediation_required','Remediation task has not passed its normal gates')
        need(not self.g.check_current(task['id']),'stale_remediation','Remediation evidence is stale')
        with self.s.transaction():
            self.s.execute("UPDATE waivers SET status='closed' WHERE id=?",(waiver,))
            self.s.execute("UPDATE inbox SET status='acknowledged' WHERE kind='waiver' AND ref=?",(waiver,))
            self.sec.event(row['project'],'waiver_resolved',actor.id,{'waiver':waiver,'remediation':task['id']})
        return {'id':waiver,'status':'closed','history_preserved':True}

    def recent_decisions(self,actor,project,since=0):
        self.k.project(actor,project)
        rows=self.s.all('SELECT * FROM decisions WHERE project=? AND created>=? ORDER BY created DESC LIMIT 500',(project,since))
        return {'decisions':[{**r,'body':parse_json(r['body'])} for r in rows],
                'unresolved_exceptions':self.s.all("SELECT id,subject,criterion,status,expires FROM waivers WHERE project=? AND status!='closed'",(project,)),
                'note':'Displayed does not mean approved; corrections preserve prior decisions and invalidate affected work.'}
