"""D01 — immutable incoming intent and a non-suppressible decision inbox."""
from __future__ import annotations
from .common import Actor, Fault, canonical, need, parse_json, text, timestamp

class Interaction:
    def __init__(self,store,security,knowledge,planning,workflow,governance):
        self.s,self.sec,self.k,self.p,self.w,self.g=store,security,knowledge,planning,workflow,governance;self.nav=None

    def intake(self,actor,content,project=None,name='New project',bounded=False):
        actor.require('owner','agent',project=project)
        need(type(bounded) is bool,'invalid_option','bounded must be boolean')
        need(not bounded or self.nav is not None,'navigation_unavailable','Bounded notification service is not connected')
        if project is None:project=self.k.create_project(actor,name)['id']
        source=self.k.source(actor,project,content,'conversation')
        warnings=self.nav.inbox(actor,project,limit=30) if bounded else self.inbox(actor,project)
        existing=self.s.one("SELECT id FROM programs WHERE project=? AND phase!='delivery' ORDER BY created DESC LIMIT 1",(project,))
        program={'id':existing['id']} if existing else self.p.begin(actor,project,source['id'],compact=bounded)
        return {'project':project,'source':source,'workflow':program,'mandatory_notifications':warnings,
                'human_authority':source['trust']=='human','instruction':'Discuss missing product meaning; keep speculative alternatives as drafts.'}

    def inbox(self,actor,project):
        self.k.project(actor,project)
        with self.s.transaction():
            rows=self.s.all("SELECT * FROM inbox WHERE project=? AND status='open' ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,created",(project,))
            for row in rows:self.s.execute('UPDATE inbox SET displayed=displayed+1 WHERE id=?',(row['id'],))
            if rows:self.sec.event(project,'inbox_displayed',actor.id,{'ids':[r['id'] for r in rows],'is_acknowledgement':False})
        return [{'id':r['id'],'kind':r['kind'],'ref':r['ref'],'severity':r['severity'],'body':parse_json(r['body']),'due':r['due'],'status':r['status']} for r in rows]

    def acknowledge(self,actor,item,utterance):
        actor.require('owner');text(utterance,'acknowledgement',10000)
        row=self.s.one('SELECT * FROM inbox WHERE id=?',(item,),True)
        need(row['kind'] not in {'product_decision','conflict'},'adjudication_required','Use the bound decision response workflow, not a generic acknowledgement')
        with self.s.transaction():
            source=self.k.source(actor,row['project'],utterance,'inbox-ack:'+item)
            self.k.classify(actor,source['id'],0,source['characters'],'reference',[],'Acknowledgement, not a new functional requirement')
            self.s.execute("UPDATE inbox SET status='acknowledged' WHERE id=?",(item,))
            self.sec.event(row['project'],'inbox_acknowledged',actor.id,{'id':item,'source':source['id'],'waiver_resolved':False})
        return {'id':item,'acknowledged':True,'exceptions_still_visible_in_status':True}

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
