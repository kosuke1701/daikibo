"""Audited public-input research and owner-configured, reconciled GitHub output."""
from __future__ import annotations
import base64
import html.parser
import http.client
import ipaddress
import os
import re
import socket
import threading
import ssl
import urllib.parse
from pathlib import Path
from .common import Actor,Fault,atomic_write,canonical,digest,need,obj,parse_json,text,timestamp,uid
from .gitops import git

class TextHTML(html.parser.HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.skip+=1
        if tag in {'p','div','br','li','h1','h2','h3','pre','tr'}:self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style'} and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)


def public_http(url,method='GET',body=None,headers=None,max_bytes=8*1024*1024,allowed_origin=None):
    """Pin resolved public IPs for this TLS connection; no environment proxy or credential redirects."""
    for redirect in range(5):
        parsed=urllib.parse.urlsplit(url)
        need(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password,'invalid_url','Only public HTTPS sources are allowed')
        origin=f'https://{parsed.netloc}'
        if allowed_origin:need(origin==allowed_origin,'forbidden','Remote operation is restricted to its configured origin')
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)
        need(addresses,'network_unavailable','Configured host did not resolve')
        if not allowed_origin:
            need(all(ipaddress.ip_address(a[4][0]).is_global for a in addresses),'private_network_denied','Public research fetch requires a public address')
        connection=http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,timeout=30,context=ssl.create_default_context())
        raw=socket.create_connection((addresses[0][4][0],parsed.port or 443),timeout=30)
        connection.sock=ssl.create_default_context().wrap_socket(raw,server_hostname=parsed.hostname)
        try:
            path=urllib.parse.urlunsplit(('', '',parsed.path or '/',parsed.query,''))
            connection.request(method,path,body=body,headers={'User-Agent':'daikibo_dev/0.1',**(headers or {})})
            response=connection.getresponse()
            if response.status in {301,302,303,307,308}:
                need(method=='GET' and not headers,'redirect_denied','Authenticated/writing requests never follow redirects')
                location=response.getheader('Location');need(location,'invalid_redirect','No redirect location');url=urllib.parse.urljoin(url,location);continue
            data=response.read(max_bytes+1);need(len(data)<=max_bytes,'too_large','HTTP response exceeds configured limit')
            return {'status':response.status,'headers':dict(response.getheaders()),'body':data,'url':url}
        finally:connection.close()
    raise Fault('redirect_loop','Too many redirects')

class Integrations:
    def __init__(self,c):
        self.c=c;self.s=c.s;self.http=public_http
        self.publish_lock=threading.RLock()

    def fetch(self,actor,project,url,purpose):
        actor.require('owner','agent',project=project);self.c.k.project(actor,project);text(purpose,'research purpose',5000)
        try:response=self.http(url)
        except (OSError,ssl.SSLError,http.client.HTTPException) as exc:
            self.c.sec.event(project,'external_read_failed',actor.id,{'url':url,'reason':type(exc).__name__})
            raise Fault('network_unavailable','Public source could not be fetched',{'url':url,'reason':type(exc).__name__}) from exc
        need(response['status']==200,'http_error','Research source did not return success',response['status'])
        media=response['headers'].get('Content-Type',response['headers'].get('content-type',''))
        raw=response['body'];blob=self.s.blob_put(raw)
        if 'html' in media:
            parser=TextHTML();parser.feed(raw.decode(errors='replace'));content=''.join(parser.parts)
        elif any(t in media for t in ('text/','json','xml')):content=raw.decode(errors='replace')
        else:raise Fault('unsupported_media','Attach/extract binary documentation through an approved parser; do not invent text',{'blob':blob,'type':media})
        # A fetched page is not an authenticated user requirement, even if the owner requested the fetch.
        agent=Actor('research:'+actor.id,'agent',project);source=self.c.k.source(agent,project,content,response['url'])
        self.c.k.classify(agent,source['id'],0,len(content),'reference',[],purpose)
        self.c.sec.event(project,'external_source_observed',actor.id,{'source':source['id'],'url':response['url'],'raw_blob':blob,'media_type':media,'purpose':purpose})
        return {**source,'url':response['url'],'raw_blob':blob,'media_type':media,'trust':'external_untrusted_reference','sample':content[:12000]}

    def configure(self,actor,project,repo,body,token=None):
        from .remote_repositories import normalized_target
        actor.require('owner',project=project)
        self.s.one('SELECT id FROM repos WHERE id=? AND project=?',(repo,project),True)
        target=normalized_target(body)
        secret_path=self.s.home/'provider-secrets'/(target['kind']+'-'+repo)
        if token is not None:
            text(token,'Repository credential',16000)
            need('\n' not in token and '\r' not in token,'invalid_remote','Credential contains a newline')
            atomic_write(secret_path,token.encode())
        need(secret_path.is_file(),'credential_required','Repository API needs a configured credential')
        record={**target,'secret_name':secret_path.name}
        with self.s.transaction():
            self.s.execute('INSERT INTO remote_targets VALUES(?,?,?) ON CONFLICT(project,repo) DO UPDATE SET body=excluded.body',
                           (project,repo,canonical(record).decode()))
            self.c.sec.event(project,'remote_target_configured',actor.id,target)
        return {'project':project,'repo':repo,'target':target}

    def status(self,actor,delivery):
        row=self.s.one('SELECT project,body FROM deliveries WHERE id=?',(delivery,),True)
        self.c.k.project(actor,row['project'])
        pending=self.s.all("SELECT id,body,status,attempts,result FROM outbox WHERE project=? AND kind='remote_publish' AND json_extract(body,'$.delivery')=? ORDER BY id",(row['project'],delivery))
        observed=self.s.all('SELECT body,status,created FROM remote_receipts WHERE delivery=? ORDER BY created,id',(delivery,))
        for value in pending:
            value['body']=parse_json(value['body'])
            if value['result']:value['result']=parse_json(value['result'])
        for value in observed:value['body']=parse_json(value['body'])
        expected=sorted(parse_json(row['body']).get('git',{}))
        recorded=sorted({v['body']['repo'] for v in observed})
        return {'delivery':delivery,'intents':pending,'observations':observed,
                'expected_repositories':expected,'historically_observed_repositories':recorded,
                'unobserved_repositories':sorted(set(expected)-set(recorded)),
                'historical_only':True,'production_deployment_performed':False}

    def publish(self,actor,delivery,title,body):
        from .remote_repositories import RepositoryAPI
        text(title,'PR/MR title',500);text(body,'PR/MR body',50000)
        with self.publish_lock:
            row,record=self.c.d.current(delivery)
            actor.require('owner',project=row['project'])
            need(row['status']=='delivered','not_delivered','Only certified, committed snapshots may be published')
            results=[]
            for repo,commit in record['git'].items():
                cfg=self.s.one('SELECT body FROM remote_targets WHERE project=? AND repo=?',(row['project'],repo),True)
                target=parse_json(cfg['body']);secret=(self.s.home/'provider-secrets'/target['secret_name']).read_text()
                api=RepositoryAPI(target,secret,self.http)
                branch='daikibo/'+delivery.lower();marker='<!-- daikibo-delivery:'+delivery+':'+repo+' -->'
                public_target={k:v for k,v in target.items() if k!='secret_name'}
                intent={'delivery':delivery,'repo':repo,'commit':commit['commit'],'branch':branch,
                        'target':public_target,'title':title,'body':body,'body_digest':digest(body)}
                dedup='publish:'+delivery+':'+repo
                with self.s.transaction():
                    previous=self.s.one('SELECT * FROM outbox WHERE dedup=?',(dedup,))
                    if previous:
                        need(parse_json(previous['body'])==intent,'remote_intent_conflict',
                             'Existing publish intent differs; reconcile it before changing target or request text')
                    else:self.c.w.emit(row['project'],'remote_publish',dedup,intent)
                    self.s.execute("UPDATE outbox SET attempts=attempts+1 WHERE dedup=?",(dedup,))
                def current():
                    now,snapshot=self.c.d.current(delivery)
                    need(now['status']=='delivered' and snapshot['git'].get(repo)==commit,'stale_delivery','Delivery changed during publication')
                    actual=self.s.one('SELECT body FROM remote_targets WHERE project=? AND repo=?',(row['project'],repo),True)
                    need(parse_json(actual['body'])==target,'remote_target_changed','Remote target changed during publication')
                    self.c.d.certify(actor,delivery,check_only=True)
                try:
                    current();api.identity()
                    observed_branch=api.branch(branch)
                    if observed_branch is None:
                        current();api.push(commit,branch);observed_branch=api.branch(branch)
                    need(observed_branch==commit['commit'],'remote_conflict','Remote branch differs; force push is never used')
                    candidates=api.requests(branch)
                    need(len(candidates)<=1,'remote_conflict','Several remote requests exist for this delivery branch')
                    if candidates:
                        number=api.number(candidates[0])
                    else:
                        current()
                        number=api.number(api.create(branch,title,body+'\n\n'+marker))
                    # A successful POST is not completion: read back the exact head, target and marker.
                    observed=api.observe(api.detail(number),branch,commit['commit'],marker)
                    need(api.branch(branch)==commit['commit'],'remote_conflict','Branch changed during remote observation')
                    current()
                    observed.update({'repo':repo,'commit':commit['commit'],'branch':branch,'delivery':delivery,
                                     'marker':marker,'target_digest':digest(public_target)})
                    with self.s.transaction():
                        previous=self.s.one('SELECT result FROM outbox WHERE dedup=?',(dedup,),True)
                        if not previous['result'] or parse_json(previous['result'])!=observed:
                            self.s.execute('INSERT INTO remote_receipts VALUES(?,?,?,?,?,?,?)',
                                (uid('REMOTE'),row['project'],delivery,repo,canonical(observed).decode(),'observed',timestamp()))
                        self.s.execute("UPDATE outbox SET status='delivered',result=? WHERE dedup=?",(canonical(observed).decode(),dedup))
                        self.c.sec.event(row['project'],'pull_request_observed','remote-publisher',observed)
                    results.append(observed)
                except (Fault,OSError,http.client.HTTPException) as exc:
                    code=exc.code if isinstance(exc,Fault) else 'remote_observation_pending'
                    with self.s.transaction():
                        self.s.execute("UPDATE outbox SET status='pending' WHERE dedup=?",(dedup,))
                        self.c.sec.event(row['project'],'remote_publish_needs_reconciliation',actor.id,
                            {'delivery':delivery,'repo':repo,'code':code,'provider':target['kind']})
                    raise
            return {'delivery':delivery,'pull_requests':results,'production_deployment_performed':False}
