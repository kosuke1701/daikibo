"""D06 adapter contracts for real Claude Code/Codex CLIs and explicit test doubles."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from .common import Fault, canonical, digest, need, obj, parse_json, strings, text
from .review_contract import REVIEW_SCHEMA, review_schema, validate_execution_control_dispositions

REVIEW_ROLES={'spec','quality','test_adequacy','specialist','requirements','design','consistency','trace','impact','feasibility','phase','test_plan','integration','goal_validation','delivery_profile','adapter_qualification','decision_proposal','execution_control','domain_responsibility'}

class Adapters:
    def __init__(self,store,security,mode): self.s,self.sec,self.mode=store,security,mode

    def register(self,actor,name,kind,executable,extra_args=None,provider=None,model=None):
        actor.require('owner')
        need(kind in {'claude','codex','fixture'},'invalid_adapter','Supported adapters: claude, codex, fixture')
        need(kind!='fixture' or self.mode=='validation','fixture_forbidden','Fixture adapters cannot be installed in governed mode')
        text(name,'adapter name',100);strings(extra_args or [],'extra_args')
        path=Path(shutil.which(executable) or executable).resolve()
        need(path.is_file() and os.access(path,os.X_OK),'adapter_unavailable','Executable not installed',str(path))
        probe=subprocess.run([str(path),'--version'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
        need(probe.returncode==0,'adapter_probe_failed','Version probe failed')
        data={'kind':kind,'executable':str(path),'sha256':digest(path.read_bytes()),'version':probe.stdout.decode(errors='replace')[:1000].strip(),
              'extra_args':extra_args or [],'provider':provider,'model':model,'simulated':kind=='fixture'}
        with self.s.transaction():
            self.s.execute("INSERT INTO adapters(name,body,qualified) VALUES(?,?,0) ON CONFLICT(name) DO UPDATE SET body=excluded.body,qualified=0,receipt=NULL",(name,canonical(data).decode()))
            self.sec.event(None,'adapter_registered',actor.id,{'name':name,**data})
        return {'name':name,**data,'qualification':'not_yet_executed'}

    def get(self,name):
        row=self.s.one("SELECT * FROM adapters WHERE name=?",(name,),True);body=parse_json(row['body'])
        path=Path(body['executable'])
        need(path.exists() and digest(path.read_bytes())==body['sha256'],'adapter_changed','Registered executable changed; re-register and requalify')
        body['qualified']=bool(row['qualified']);body['name']=name
        return body

    def command(self,adapter,role,work:Path,home:Path):
        review=role in REVIEW_ROLES
        schema=review_schema(role) if review else None
        if adapter['kind']=='fixture':
            return [adapter['executable'],*adapter['extra_args']],None
        if adapter['kind']=='claude':
            args=[adapter['executable'],'-p','--output-format','json',
                  '--permission-mode','bypassPermissions','--no-session-persistence']
            if schema is not None:args+=['--json-schema',canonical(schema).decode()]
            if adapter.get('model'):args+=['--model',adapter['model']]
            return args+adapter['extra_args'],None
        schema_path=home/'review-schema.json';output=home/'final.json'
        if schema is not None:schema_path.write_bytes(canonical(schema))
        args=[adapter['executable'],'exec','--json','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','--output-last-message',str(output)]
        if schema is not None:args+=['--output-schema',str(schema_path)]
        if adapter.get('model'):args+=['--model',adapter['model']]
        return args+adapter['extra_args']+['-'],output

    @staticmethod
    def normalize(adapter,stdout:bytes,result_file:Path|None=None):
        from .execution_errors import decode
        result,metadata,failed=decode(adapter['kind'],stdout,result_file)
        if failed:
            raise Fault('agent_failed','CLI returned a failed terminal result',{'failure':failed,'metadata':metadata})
        return result,metadata

    @staticmethod
    def validate_review(result, role=None):
        obj(result,required=('verdict','rationale','covered','findings','observations','dispositions'))
        need(result['verdict'] in {'pass','fail','blocked'},'invalid_review','Unknown verdict')
        text(result['rationale'],'review rationale',40000);strings(result['covered'],'coverage')
        need(isinstance(result['findings'],list) and isinstance(result['observations'],list) and result['observations'],'invalid_review','Review must include actual observations')
        for item in result['findings']:
            obj(item,required=('severity','statement','evidence'))
            need(item['severity'] in {'low','medium','high','critical'},'invalid_review','Invalid severity')
            text(item['statement']);text(item['evidence'])
        for item in result['observations']:
            obj(item,required=('ref','detail'));text(item['ref']);text(item['detail'])
        need(isinstance(result['dispositions'],list),'invalid_review','Invalid dispositions')
        for item in result['dispositions']:
            obj(item,required=('id','resolution','reason'));text(item['id']);text(item['resolution']);text(item['reason'])
        if role == 'execution_control':
            validate_execution_control_dispositions(result['dispositions'])
        need(result['verdict']!='pass' or not result['findings'],'invalid_review','PASS cannot contain unresolved findings')
        return result
