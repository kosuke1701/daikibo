"""Durable bounded jobs, finite read-only retries and observed progress budgets."""
from __future__ import annotations
import concurrent.futures
import dataclasses
import threading
import time
from .common import Actor,Fault,canonical,digest,need,number,obj,parse_json,text,timestamp,uid

KINDS={'execute','review','tests','index','delivery.verify','supervisor.turn','adapter.qualify','remote.publish','ops.backup','ops.audit','ops.gc','traceability.extract'}
RETRYABLE_KINDS={'review','supervisor.turn'}
DEFAULT_RETRY={'max_attempts':3,'base_delay_seconds':10.0,'max_delay_seconds':300.0,'max_elapsed_seconds':1800.0}


def retry_policy(kind, value=None):
    value={} if value is None else value
    obj(value,optional=tuple(DEFAULT_RETRY),name='retry policy')
    result={**DEFAULT_RETRY,**value}
    if kind not in RETRYABLE_KINDS:
        result['max_attempts']=value.get('max_attempts',1)
        need(result['max_attempts']==1,'unsafe_retry','Mutating or multi-step jobs require explicit reassessment, not automatic retry')
    number(result['max_attempts'],'max_attempts',1,10,integer=True)
    number(result['base_delay_seconds'],'base_delay_seconds',.01,3600)
    number(result['max_delay_seconds'],'max_delay_seconds',result['base_delay_seconds'],86400)
    number(result['max_elapsed_seconds'],'max_elapsed_seconds',.01,604800)
    return result


class Jobs:
    def __init__(self,c):
        self.c=c;self.s=c.s;self.stop_event=threading.Event();self.thread=None
        self.pool=concurrent.futures.ThreadPoolExecutor(max_workers=32,thread_name_prefix='daikibo-job')
        self.active={};self.lock=threading.RLock();self.next_evidence_sweep=0.0

    def subject_project(self,kind,args):
        if kind in {'execute','tests'}:
            return self.s.one('SELECT project FROM tasks WHERE id=?',(args.get('task'),),True)['project']
        if kind=='index':return self.s.one('SELECT project FROM repos WHERE id=?',(args.get('repo'),),True)['project']
        if kind=='traceability.extract':return self.s.one('SELECT project FROM traceability_proposals WHERE id=?',(args.get('proposal'),),True)['project']
        if kind in {'ops.backup','ops.audit','ops.gc'}:return None
        if kind in {'delivery.verify','remote.publish'}:return self.s.one('SELECT project FROM deliveries WHERE id=?',(args.get('delivery'),),True)['project']
        if kind=='review':
            subject=args.get('subject')
            returned=self.s.one('SELECT project FROM scope_return_packets WHERE id=?',(subject,))
            if returned:return returned['project']
            trace=self.s.one("SELECT project FROM traceability_records WHERE id=? AND kind='review_packet'",(subject,))
            if trace:return trace['project']
            if self.s.one('SELECT id FROM projects WHERE id=?',(subject,)):return subject
            row=self.s.one('SELECT w.project FROM workstream_packets p JOIN workstreams w ON w.id=p.scope WHERE p.id=?',(subject,))
            if row:return row['project']
            row=self.s.one('SELECT project FROM local_execution_packets WHERE id=?',(subject,))
            if row:return row['project']
            # Assurance review packets are immutable, typed subjects.  Keep
            # this resolver aligned with Runtime._subject: an arbitrary
            # assurance object is not a review job subject, while a canonical
            # packet is routed to its recorded project and still undergoes
            # the normal role/currentness checks during fingerprinting and
            # execution.
            row=self.s.one("SELECT project FROM assurance_objects WHERE id=? AND kind='packet'",(subject,))
            if row:return row['project']
            for table in ('tasks','artifacts','changes','decisions','decision_batches','programs','deliveries','review_scopes','breakdown_packets','subplan_packets','task_revision_proposals','workstreams','local_execution_proposals','execution_control_proposals','traceability_records'):
                row=self.s.one(f'SELECT project FROM {table} WHERE id=?',(subject,))
                if row:return row['project']
            raise Fault('not_found','Review subject does not exist')
        return args.get('project')

    def _fingerprint(self,actor,kind,args):
        if kind not in RETRYABLE_KINDS:return None
        ad=self.c.rt.adapters.get(args['adapter'])
        if kind=='review':
            project,binding,snapshot,context,task=self.c.rt._subject(actor,args['subject'],args['role'],args.get('proposal'))
            state={'binding':binding,'snapshot':snapshot['digest'],'context':digest(context),'task_epoch':task['epoch'] if task else None}
        else:
            project=args['project'];state={'state':self.c.supervisor.state_digest(project)}
        provider=self.s.one('SELECT body FROM providers WHERE name=?',(ad.get('provider'),)) if ad.get('provider') else None
        return digest({'state':state,'adapter':{k:v for k,v in ad.items() if k!='qualified'},
                       'provider':provider,'policy':self.c.g.policy(project)['digest']})

    def submit(self,actor,kind,args,dedup=None,retry=None):
        need(kind in KINDS,'invalid_job','Unknown long-running operation');need(isinstance(args,dict),'invalid_params','Job arguments must be an object')
        project=self.subject_project(kind,args)
        actor.require('owner','agent',project=project)
        if project:self.c.k.project(actor,project)
        if kind in {'remote.publish','adapter.qualify','ops.backup','ops.audit','ops.gc'}:actor.require('owner',project=project)
        if kind=='execute':
            row=self.c.w.task(actor,args['task']);need(row['status']=='running','lease_required','Claim work before submitting execution')
        ident=uid('JOB');body=canonical(args).decode();policy=retry_policy(kind,retry);now=timestamp()
        with self.s.transaction():
            if dedup:
                existing=self.s.one('SELECT * FROM jobs WHERE dedup=?',(dedup,))
                if existing:
                    need(existing['kind']==kind and existing['args']==body and existing['project']==project,'idempotency_conflict','Job key reused for another operation')
                    if retry is not None:
                        need(parse_json(existing['retry_policy'])==policy,'idempotency_conflict','Retry policy changed for an existing request')
                    return self.get(actor,existing['id'])
            fingerprint=self._fingerprint(actor,kind,args)
            self.s.execute("INSERT INTO jobs(id,project,kind,actor,args,status,dedup,created,retry_policy,retry_deadline,retry_fingerprint) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)",
                           (ident,project,kind,canonical(dataclasses.asdict(actor)).decode(),body,dedup,now,canonical(policy).decode(),now+policy['max_elapsed_seconds'],fingerprint))
            self.c.sec.event(project,'job_queued',actor.id,{'job':ident,'kind':kind,'retry_policy':policy,'input_fingerprint':fingerprint})
        return {'id':ident,'status':'queued','project':project,'retry_policy':policy}

    def get(self,actor,job):
        row=self.s.one('SELECT * FROM jobs WHERE id=?',(job,),True)
        if row['project']:self.c.k.project(actor,row['project'])
        else:actor.require('owner')
        for k in ('args','result','error','retry_policy'):row[k]=parse_json(row[k]) if row[k] else None
        row['attempts']=self.s.all('SELECT * FROM job_attempts WHERE job=? ORDER BY attempt',(job,))
        for attempt in row['attempts']:
            for key in ('result','error'):attempt[key]=parse_json(attempt[key]) if attempt[key] else None
        row.pop('actor',None);return row

    def list(self,actor,project,limit=100,offset=0,kind=None,status=None,task=None,subject=None,since=0,until=None,expected_snapshot=None):
        self.c.k.project(actor,project);need(type(limit) is int and 1<=limit<=1000,'invalid_limit','Invalid job page size')
        need(type(offset) is int and offset>=0,'invalid_range','Invalid job offset')
        if kind is not None: need(kind in KINDS,'invalid_job','Unknown job kind')
        if status is not None: need(status in {'queued','running','retry_wait','succeeded','failed','unknown','cancelled'},'invalid_state','Unknown job status')
        for value in (task,subject):
            if value is not None: text(value,'subject filter',300)
        number(since,'since',0,1e20)
        if until is not None: number(until,'until',since,1e20)
        with self.s.transaction():
            rows=self.s.all('''SELECT id,kind,status,created,started,ended,cancelled,attempt_count,retry_due
                FROM jobs WHERE project=? AND (? IS NULL OR kind=?) AND (? IS NULL OR status=?)
                AND (? IS NULL OR json_extract(args,'$.task')=?)
                AND (? IS NULL OR coalesce(json_extract(args,'$.subject'),json_extract(args,'$.task'),json_extract(args,'$.delivery'),json_extract(args,'$.repo'))=?)
                AND created>=? AND (? IS NULL OR created<=?) ORDER BY created DESC,id DESC''',
                (project,kind,kind,status,status,task,task,subject,subject,since,until,until))
            stamp=digest({'project':project,'kind':kind,'status':status,'task':task,'subject':subject,'since':since,'until':until,'items':rows})
            need(not offset or expected_snapshot is not None,'snapshot_required','Continue with the previous snapshot')
            need(expected_snapshot is None or expected_snapshot==stamp,'stale_catalog','Job catalog changed; restart the filtered query')
            return {'jobs':rows[offset:offset+limit],'total':len(rows),'snapshot':stamp,
                    'next_offset':offset+limit if offset+limit<len(rows) else None}

    def retry(self,actor,job,reason):
        old=self.get(actor,job);actor.require('owner','agent',project=old['project']);text(reason,'retry/reconciliation reason',4000)
        need(old['status'] in {'failed','unknown'},'invalid_state','Only failed/unknown jobs can be explicitly retried')
        need(old['kind'] in RETRYABLE_KINDS,'reassessment_required','Implementation and external side effects require task/change reconciliation, then a new job')
        with self.s.transaction():
            result=self.submit(actor,old['kind'],old['args'],dedup='manual:'+uid('RETRY'),retry=old['retry_policy'])
            self.c.sec.event(old['project'],'job_retry_requested',actor.id,{'old_job':job,'new_job':result['id'],'reason':reason,'old_results_preserved':True})
        return {**result,'retry_of':job}

    def cancel(self,actor,job,reason):
        text(reason,'cancellation reason',4000)
        with self.s.transaction():
            row=self.s.one('SELECT * FROM jobs WHERE id=?',(job,),True)
            actor.require('owner','agent',project=row['project'])
            if row['status'] not in {'queued','running','retry_wait'}:
                return {'id':job,'cancel_requested':False,'status':row['status'],
                        'warning':'Work already has a terminal outcome; record a compensating change instead of cancelling history.'}
            args=parse_json(row['args'])
            self.s.execute("UPDATE jobs SET cancelled=1,status=CASE WHEN status IN ('queued','retry_wait') THEN 'cancelled' ELSE status END,retry_due=NULL,ended=CASE WHEN status IN ('queued','retry_wait') THEN ? ELSE ended END WHERE id=?",(timestamp(),job))
            if row['kind'] in {'execute','tests'} and row['status']=='running':
                self.c.w.pause(actor,row['project'],task=args['task'],fence=True)
            self.c.sec.event(row['project'],'job_cancel_requested',actor.id,{'job':job,'reason':reason})
        return {'id':job,'cancel_requested':True,'warning':'A completed external side effect is not silently rolled back.'}

    def _actor(self,row):
        return Actor(**parse_json(row['actor']))

    def run_one(self,row):
        current=self.s.one('SELECT * FROM jobs WHERE id=?',(row['id'],),True)
        if current['kind']!='index' or current['status'] not in {'queued','retry_wait'} or current['cancelled']:
            return self._run_one(current)
        # Reserve the same slot used by direct scans BEFORE starting an attempt.
        # Contention leaves the durable Job queued, without consuming retries.
        if not self.c.idx.scan_lock.acquire(blocking=False):
            return {'status':current['status'],'result':None,'error':None,'waiting_for':'index'}
        try: return self._run_one(current)
        finally: self.c.idx.scan_lock.release()

    def _run_one(self,row):
        # Atomic dispatch prevents two scheduler callbacks from running the same attempt.
        with self.s.transaction():
            current=self.s.one('SELECT * FROM jobs WHERE id=?',(row['id'],),True);now=timestamp()
            if current['cancelled'] and (current['status'] in {'queued','retry_wait'} or (current['status']=='running' and current['attempt_count']==0)):
                self.s.execute("UPDATE jobs SET status='cancelled',ended=? WHERE id=?",(now,row['id']))
                current['status']='cancelled'
            if current['status']=='retry_wait' and current['retry_due']>now:
                return {'status':'retry_wait','retry_due':current['retry_due'],'result':None,'error':None,'replayed':True}
            if current['cancelled'] or current['status'] not in {'queued','retry_wait'}:
                return {'status':current['status'],'result':parse_json(current['result']) if current['result'] else None,
                        'error':parse_json(current['error']) if current['error'] else None,'replayed':True}
            if current['project'] and self.s.one('SELECT paused FROM projects WHERE id=?',(current['project'],),True)['paused']:
                return {'status':current['status'],'result':None,'error':None,'paused':True}
            row=current;attempt=row['attempt_count']+1
            self.s.execute("UPDATE jobs SET status='running',started=coalesce(started,?),attempt_count=?,retry_due=NULL WHERE id=?",(now,attempt,row['id']))
            self.s.execute("INSERT INTO job_attempts(job,attempt,status,started) VALUES(?,?,'running',?)",(row['id'],attempt,now))
        self.c.rt.job_context.id=row['id'];self.c.rt.job_context.last_receipt=None
        start=time.monotonic();result=None;error=None;state='succeeded';observed=None
        try:
            actor=self._actor(row);args=parse_json(row['args']);kind=row['kind']
            if row['retry_fingerprint']:
                need(self._fingerprint(actor,kind,args)==row['retry_fingerprint'],'retry_stale','Inputs or adapter/policy changed; replan before another attempt')
            # Expiry only governs auto-retries; initial queued work can still start normally.
            if attempt>1:
                need(row['retry_deadline'] is None or timestamp()<=row['retry_deadline'],'retry_expired','Retry window expired')
            fn={'execute':self.c.rt.execute,'review':self.c.rt.review,'tests':self.c.rt.tests,'index':self.c.idx._scan,
                'delivery.verify':self.c.d.verify,'supervisor.turn':self.c.supervisor.turn,'adapter.qualify':self.c.supervisor.qualify,
                'remote.publish':self.c.external.publish,'ops.backup':self.c.ops.backup,'ops.audit':self.c.ops.audit,'ops.gc':self.c.ops.garbage_collect,
                'traceability.extract':self.c.traceability.extract}[kind]
            result=fn(actor,**args)
            observed=getattr(self.c.rt.job_context,'last_receipt',None)
            if observed and isinstance(result,dict):
                # Public job callers get the same collector-owned capture
                # envelope as the receipt.  The worker's formal result object
                # remains unchanged; this is an additive job-level field.
                result={**result,'output_capture':observed.get('output_capture')}
            if kind in RETRYABLE_KINDS and observed and observed.get('failure'):
                raise Fault('agent_execution_failed','Agent execution failed; no engineering judgment was adopted',
                            {'receipt':observed['id'],'failure':observed['failure']})
        except Fault as exc:state='failed';error=exc.as_dict()
        except Exception as exc:
            state='failed';error={'code':'internal_error','message':type(exc).__name__+': '+str(exc)[:2000]}
        finally:
            observed=getattr(self.c.rt.job_context,'last_receipt',None)
            self.c.rt.job_context.id=None;self.c.rt.job_context.last_receipt=None
        if observed and state=='failed':
            error={**(error or {}),'receipt':observed['id'],'failure':observed.get('failure'),'partial_work':observed.get('partial_work') or (observed.get('work_product') if row['kind']=='execute' else None)}
            error['output_capture']=observed.get('output_capture')
        due=None;retry_reason=None
        policy=parse_json(row['retry_policy']) if row['retry_policy']!='{}' else retry_policy(row['kind'])
        fail=observed.get('failure') if observed else None
        if state=='failed' and row['kind'] in RETRYABLE_KINDS and fail and fail.get('retryable') and observed.get('readonly_verified'):
            if attempt>=policy['max_attempts']:retry_reason='attempt_limit'
            else:
                delay=policy['base_delay_seconds']*(2**(attempt-1))
                delay*=1+(int(digest({'job':row['id'],'attempt':attempt})[:6],16)%1000)/5000
                delay=max(delay,fail.get('retry_after_seconds') or 0)
                if delay>policy['max_delay_seconds']:retry_reason='reported_wait_exceeds_limit'
                elif timestamp()+delay>(row['retry_deadline'] or 0):retry_reason='retry_window_exhausted'
                else:due=timestamp()+delay;state='retry_wait'
        with self.s.transaction():
            current=self.s.one('SELECT cancelled FROM jobs WHERE id=?',(row['id'],),True)
            if current['cancelled']:state='cancelled';due=None
            self.s.execute('UPDATE job_attempts SET status=?,ended=?,result=?,error=? WHERE job=? AND attempt=?',
                           ('failed' if state=='retry_wait' else state,timestamp(),canonical(result).decode() if result is not None else None,canonical(error).decode() if error else None,row['id'],attempt))
            self.s.execute('UPDATE jobs SET status=?,ended=?,result=?,error=?,retry_due=? WHERE id=?',
                           (state,None if state=='retry_wait' else timestamp(),canonical(result).decode() if result is not None else None,canonical(error).decode() if error else None,due,row['id']))
            self.c.sec.event(row['project'],'job_attempt_finished','scheduler',{'job':row['id'],'attempt':attempt,'status':state,'error':error,'retry_due':due,'retry_stopped_because':retry_reason})
            if row['project']:
                self.s.execute('UPDATE automation SET spent=spent+?,failures=failures+?,last_progress=CASE WHEN ? THEN ? ELSE last_progress END WHERE project=?',
                               (time.monotonic()-start,1 if state=='failed' else 0,state=='succeeded',timestamp(),row['project']))
                if state=='failed':
                    args=parse_json(row['args']);ref=args.get('task',row['id'])
                    self.c.g.inbox(row['project'],'job_failure',ref,{'job':row['id'],'error':error,'retry_stopped_because':retry_reason,'technical_impossibility':False,'action':'Inspect recorded execution and reassess only affected work'},'warning')
                    if row['kind']=='execute':
                        task=self.c.w.task(Actor('scheduler','owner'),args['task'])
                        if task['status']=='running':
                            self.s.execute("UPDATE tasks SET status='planned',validity='needs_review',epoch=epoch+1,lease_owner=NULL,lease_until=NULL WHERE id=?",(task['id'],))
                            self.s.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)',(task['id'],'run_unknown',row['id'],'Execution failed; inspect partial_work and reassess before repeating'))
        return {'status':state,'result':result,'error':error,'retry_due':due,'attempt':attempt}

    def tick(self):
        self.c.w.reconcile(Actor('scheduler','owner'))
        if time.monotonic()>=self.next_evidence_sweep:
            self.c.ops.reconcile_evidence(batch_size=100)
            self.next_evidence_sweep=time.monotonic()+30
        with self.lock:
            self.active={k:v for k,v in self.active.items() if not v.done()}
            available=32-len(self.active)
            rows=self.s.all("""SELECT j.* FROM jobs j LEFT JOIN projects p ON p.id=j.project
                WHERE (j.status='queued' OR (j.status='retry_wait' AND j.retry_due<=?))
                AND j.cancelled=0 AND coalesce(p.paused,0)=0 ORDER BY j.created LIMIT ?""",(timestamp(),32))
            for row in rows:
                if available<=0:break
                if row['id'] in self.active:continue
                self.active[row['id']]=self.pool.submit(self.run_one,row);available-=1
        self._automate()

    def configure(self,actor,project,adapter,reviewer,budget_seconds=14400,concurrency=4,enabled=True):
        actor.require('owner',project=project);self.c.k.project(actor,project)
        for name in (adapter,reviewer):self.c.rt.adapters.get(name)
        need(type(enabled) is bool and 1<=concurrency<=32 and 1<=budget_seconds<=604800,'invalid_automation','Invalid budget or concurrency')
        worker=Actor('automation:'+project,'agent',project);now=timestamp()
        with self.s.transaction():
            self.s.execute('INSERT INTO automation VALUES(?,?,?,?,?,?,0,?,0,?,?) ON CONFLICT(project) DO UPDATE SET enabled=excluded.enabled,actor=excluded.actor,adapter=excluded.adapter,reviewer=excluded.reviewer,budget=excluded.budget,spent=0,concurrency=excluded.concurrency,failures=0,last_progress=excluded.last_progress,body=excluded.body',
                           (project,int(enabled),canonical(dataclasses.asdict(worker)).decode(),adapter,reviewer,budget_seconds,concurrency,now,canonical({'authorized_by':actor.id,'authorized_at':now}).decode()))
            self.c.sec.event(project,'autonomy_configured',actor.id,{'enabled':enabled,'budget_seconds':budget_seconds,'concurrency':concurrency,'adapter':adapter,'reviewer':reviewer})
        return {'project':project,'enabled':enabled,'budget_seconds':budget_seconds,'concurrency':concurrency,'execution_model':'cooperative-single-user'}

    def automation_status(self,actor,project):
        self.c.k.project(actor,project);row=self.s.one('SELECT * FROM automation WHERE project=?',(project,))
        if not row:return {'configured':False}
        row.pop('actor',None);row['body']=parse_json(row['body']);return row

    def _automate(self):
        for config in self.s.all('SELECT * FROM automation WHERE enabled=1'):
            project=config['project'];actor=Actor(**parse_json(config['actor']))
            if self.s.one('SELECT paused FROM projects WHERE id=?',(project,),True)['paused']:continue
            closure=self.s.one('SELECT body FROM program_closures WHERE project=? ORDER BY created DESC LIMIT 1',(project,))
            if closure and parse_json(closure['body'])['engineering_digest']==self.c.supervisor.state_digest(project):
                # Do not manufacture work or a no-progress warning after a valid
                # recorded closure. New user input/state invalidates this match.
                continue
            if config['spent']>=config['budget'] or config['failures']>=10:
                with self.s.transaction():
                    self.s.execute('UPDATE automation SET enabled=0 WHERE project=?',(project,))
                    self.c.g.inbox(project,'autonomy_limit',project,{'reason':'budget_exhausted' if config['spent']>=config['budget'] else 'failure_budget','technical_impossibility':False},'warning')
                continue
            pending=self.s.one("SELECT count(*) AS n FROM jobs WHERE project=? AND status IN ('queued','running')",(project,))['n']
            capacity=max(0,config['concurrency']-pending)
            if not capacity:continue
            # Work already sealed progresses through actual tests, separate reviews and Gate.
            for proposal in self.s.all("SELECT id,task,project,digest,binding,body FROM execution_control_proposals WHERE project=? AND status='proposed' ORDER BY created", (project,)):
                if capacity <= 0: break
                body=parse_json(proposal['body'])
                refs=self.c.g.evidence_for(proposal['id'], proposal['binding'], 'execution_control')
                # The proposal binding is stored on its row and is the only
                # review subject binding; the review job is independently
                # observed and apply remains an explicit control operation.
                if not refs:
                    self.submit(actor,'review',{'subject':proposal['id'],'role':'execution_control','adapter':config['reviewer']},'auto:execution-control-review:'+proposal['id']+':'+proposal['digest'])
                    capacity-=1
            for decision in self.s.all("SELECT id,digest FROM decisions WHERE project=? AND status='instruction_recorded' AND json_extract(body,'$.type')='execution_control_policy' ORDER BY created", (project,)):
                if capacity <= 0: break
                if not self.c.g.evidence_for(decision['id'], self.c.execution_controls.policy_binding(decision['id']), 'consistency'):
                    self.submit(actor,'review',{'subject':decision['id'],'role':'consistency','adapter':config['reviewer']},'auto:execution-policy-review:'+decision['id']+':'+decision['digest'])
                    capacity-=1
            for row in self.s.all("SELECT id,status,revision FROM tasks WHERE project=? AND validity='current' AND paused=0 AND status IN ('ready','submitted') ORDER BY created",(project,)):
                if capacity<=0:break
                try:
                    task=self.c.w.task(actor,row['id']);binding=self.c.g.task_binding(row['id'])
                    if row['status']=='ready':
                        claim=self.c.w.claim(actor,project,task=row['id'],adapter=config['adapter'])
                        if not claim:continue
                        result=self.submit(actor,'execute',{'task':row['id'],'adapter':config['adapter']},'auto:execute:'+binding)
                        capacity-=1;continue
                    tests=parse_json(self.s.one('SELECT body FROM plans WHERE task=?',(row['id'],),True)['body'])['checks']
                    selection=self.c.task_test_evidence(actor,row['id'])
                    selected={item['check_id']:item for item in selection['checks']}
                    missing=[ch for ch in tests if selected.get(ch['id'],{}).get('status')=='unobserved']
                    if missing:
                        result=self.submit(actor,'tests',{'task':row['id']},'auto:tests:'+binding);capacity-=1;continue
                    failed=[ch['id'] for ch in tests if selected.get(ch['id'],{}).get('status')!='executed']
                    roles=list(self.c.g.policy(project)['body']['review_roles'])
                    if task['body']['risk']=='critical':roles.append('specialist')
                    if not failed:
                        for role in roles:
                            refs=self.c.g.evidence_for(row['id'],binding,role)
                            if not refs:
                                self.submit(actor,'review',{'subject':row['id'],'role':role,'adapter':config['reviewer']},'auto:review:'+binding+':'+role);capacity-=1;break
                            observed=self.c.g.receipt(refs[0]['id'])
                            if observed.get('failure'):
                                # Its durable job owns retry/reconciliation; it is not a content judgment.
                                break
                            try:self.c.g.require_review(refs[0]['id'],row['id'],binding,{role})
                            except Fault:failed.append(role);break
                        else:
                            self.c.w.complete(actor,row['id'],row['revision']);continue
                    if failed:
                        with self.s.transaction():
                            self.s.execute('INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)',(row['id'],'review_failed',binding,'Failed tests or review: '+','.join(failed)))
                            self.c.g.inbox(project,'verification_failed',row['id'],{'failed':failed,'binding':binding,'action':'Reassess the lowest invalid engineering layer; do not weaken requirements or tests.'},'warning')
                except Fault as exc:
                    if exc.code not in {'capacity','dependency_not_ready','write_conflict','read_conflict','blocked'}:
                        self.c.g.inbox(project,'scheduling_block',row['id'],{'error':exc.as_dict()},'warning')
            # Let a bounded supervisor diagnose/replan; no infinite review retry.
            if capacity>0 and not self.s.one("SELECT id FROM jobs WHERE project=? AND kind='supervisor.turn' AND status IN ('queued','running','retry_wait')",(project,)):
                progress=self.c.supervisor.progress_digest(project)
                dedup='auto:supervisor:'+progress
                existing=self.s.one('SELECT status FROM jobs WHERE dedup=?',(dedup,))
                if existing is None:
                    self.submit(actor,'supervisor.turn',{'project':project,'adapter':config['adapter'],'message':'Advance the current workflow. Resolve technical issues locally before escalating product choices. Preserve all required scope.'},dedup)
                elif timestamp()-config['last_progress']>600:
                    self.c.g.inbox(project,'no_progress',project,{'reason':'No canonical artifact or engineering state changed. Review input/context, do not declare impossibility.'},'warning')

    def start(self):
        if self.thread:return
        def loop():
            while not self.stop_event.wait(0.5):
                try:self.tick()
                except Exception as exc:
                    # Retain a visible scheduler fault, not a false successful tick.
                    try:self.c.sec.event(None,'scheduler_fault','scheduler',{'type':type(exc).__name__,'message':str(exc)[:2000]})
                    except Exception:pass
        self.thread=threading.Thread(target=loop,name='daikibo-scheduler',daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=3)
        self.c.rt.shutdown();self.pool.shutdown(wait=True,cancel_futures=True)
