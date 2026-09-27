"""CHG-LOCAL-001: same-user execution and real persistence/CLI protocol tests.

Fixture-agent runs validate orchestration, not the correctness of an actual LLM.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from daikibo.common import Actor, Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.hooks import dispatch, install
from daikibo.agents import Adapters
from conftest import make_task, finish_task


class LocalClient:
    def __init__(self, c): self.c=c
    def call(self, method, params=None, request_id=None):
        from daikibo.common import uid
        return self.c.request(None, {'id':request_id or uid('request'),'method':method,'params':params or {}})


def attach(c, project, session='session-one'):
    return c.native.attach(c.owner, session, str(project[3]), project=project[0])


def proposal(c, p):
    return c.p.propose_decision(c.owner,p[0],{'title':'Choose display','reason':'Needs product meaning',
        'options':['existing','new'],'recommendation':'existing','refs':[p[2]],'requirement_affecting':True})


def test_governed_initialization_requires_no_privileges_or_credentials(tmp_path):
    c=Control(tmp_path/'state',start_workers=False)
    try:
        actor=c.sec.authenticate(); info=c.ops.doctor(actor)
        assert info['execution_model']=='cooperative-single-user'
        assert info['isolation_required'] is False
        assert not (c.s.home/'owner.token').exists()
        assert not (c.s.home/'keys.json').exists()
        assert c.sec.audit()['tamper_resistant'] is False
        assert c.sec.mac({'x':1})[0]=='sha256-unkeyed-v1'
        assert 'capability.issue' not in c.routes and 'keys.rotate' not in c.routes
    finally:c.close()


def test_process_inherits_user_and_home_without_privilege_syscalls(full,full_project,monkeypatch):
    c=full; p=full_project[0]
    def prohibited(*args,**kwargs): raise AssertionError('Unexpected privilege/isolation operation')
    for name in ('setuid','setgid','setgroups','chown'):
        monkeypatch.setattr(os,name,prohibited)
    import resource
    monkeypatch.setattr(resource,'setrlimit',prohibited)
    script='import os,json;print(json.dumps({"uid":os.geteuid(),"home":os.environ["HOME"],"marker":os.environ["LOCAL_MARKER"]}))'
    monkeypatch.setenv('LOCAL_MARKER','same-user-environment')
    receipt,_,_=c.rt.observe(p,None,'same-user','probe',None,digest(script),c.sn.capture(c.owner,p),
                  lambda w,h,d:([sys.executable,'-c',script],None),timeout=5)
    result=json.loads(c.s.blob_get(receipt['stdout_blob']))
    assert receipt['exit_code']==0
    assert result=={'uid':os.geteuid(),'home':os.environ['HOME'],'marker':'same-user-environment'}
    assert receipt['tamper_resistant'] is False


def test_timeout_does_not_kill_unrelated_same_uid_process(full,full_project):
    other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])
    try:
        c=full;script='import time;time.sleep(20)'
        receipt,_,_=c.rt.observe(full_project[0],None,'cancel-only-my-run','probe',None,digest(script),c.sn.capture(c.owner,full_project[0]),
                      lambda w,h,d:([sys.executable,'-c',script],None),timeout=.1)
        assert receipt['timed_out'] and other.poll() is None
    finally:other.terminate();other.wait(timeout=3)


def test_native_attach_is_idempotent_and_shares_workspace_between_sessions(full,full_project):
    c=full; a=attach(c,full_project); b=attach(c,full_project)
    d=c.native.attach(c.owner,'new-session',str(full_project[3]))
    assert a['project']==b['project']==d['project']==full_project[0]
    assert len(c.repository_list(c.owner,full_project[0])['repositories'])==1
    with pytest.raises(Fault):c.native.attach(c.owner,'session-one',str(full_project[3].parent))


def test_native_raw_input_retained_deduplicated_only_by_turn_id(full,full_project):
    c=full;attach(c,full_project)
    a=c.native.input(c.owner,'session-one','元の仕様をそのまま保存','turn-a')
    b=c.native.input(c.owner,'session-one','元の仕様をそのまま保存','turn-a')
    assert a['source']==b['source'] and b['replayed']
    other=c.native.input(c.owner,'session-one','元の仕様をそのまま保存','turn-b')
    assert other['source']!=a['source']
    assert c.k.source_read(c.owner,a['source'])['content']=='元の仕様をそのまま保存'
    with pytest.raises(Fault):c.native.input(c.owner,'session-one','different','turn-a')


def test_native_decision_needs_shown_revision_and_subsequent_verbatim_answer(full,full_project):
    c=full;attach(c,full_project);dec=proposal(c,full_project)
    old=c.native.input(c.owner,'session-one','existingでお願いします')['source']
    with pytest.raises(Fault):c.native.respond(c.owner,'session-one',dec['id'],dec['digest'],old,'existing','existingでお願いします')
    shown=c.native.present_decision(c.owner,'session-one',dec['id'])
    with pytest.raises(Fault):c.native.respond(c.owner,'session-one',dec['id'],dec['digest'],old,'existing','existingでお願いします')
    new=c.native.input(c.owner,'session-one','existingでお願いします')['source']
    with pytest.raises(Fault):c.native.respond(c.owner,'session-one',dec['id'],dec['digest'],new,'new','newを選びます')
    received=c.native.respond(c.owner,'session-one',dec['id'],shown['expected_digest'],new,'existing','existingでお願いします')
    assert received['status']=='decision_received' and received['consistency_recheck_required']
    # A source-backed answer isn't itself a completed consistency review.
    with pytest.raises(Fault):c.p.apply_decision(c.owner,dec['id'],'invented-review')
    rev=c.rt.review(c.owner,dec['id'],'consistency','fixture')
    assert c.p.apply_decision(c.owner,dec['id'],rev['receipt'])['status']=='applied'


def test_native_cross_session_or_other_project_answer_rejected(full,full_project):
    c=full;attach(c,full_project);c.native.attach(c.owner,'another',str(full_project[3]))
    dec=proposal(c,full_project);c.native.present_decision(c.owner,'session-one',dec['id'])
    answer=c.native.input(c.owner,'another','existing')['source']
    with pytest.raises(Fault):c.native.respond(c.owner,'session-one',dec['id'],dec['digest'],answer,'existing','existing')


def test_native_actions_use_existing_gates_not_fake_receipts(full,full_project):
    c=full;attach(c,full_project)
    result=c.native.actions(c.owner,'session-one',[{'method':'artifact.propose','params':{'project':full_project[0],'kind':'finding','body':{'title':'Observation','statement':'Need discovery'}},'as':'finding'},
             {'method':'artifact.get','params':{'artifact':{'$ref':'finding.id'}}}])
    # Check callable signature: any contract error is visible, not an implicit success.
    assert result['all_applied'] and result['actions'][0].get('result',{}).get('id')
    forbidden=c.native.actions(c.owner,'session-one',[{'method':'decision.respond','params':{}}])
    assert not forbidden['all_applied'] and forbidden['actions'][0]['error']['code']=='workflow_boundary'
    t=make_task(c,full_project)
    blocked=c.native.completion(c.owner,'session-one',t)
    assert not blocked['completed']
    assert c.native.stop_feedback(c.owner,'session-one')['decision']=='block'
    assert c.native.stop_feedback(c.owner,'session-one',True)=={}
    finish_task(c,full_project[0],t)
    assert c.native.completion(c.owner,'session-one',t)['completed']


def test_install_hooks_merges_settings_idempotently(tmp_path):
    settings=tmp_path/'.claude/settings.local.json';settings.parent.mkdir()
    settings.write_text(json.dumps({'model':'user-choice','hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo keep-me'}]}]}}))
    for _ in range(2):install(tmp_path,tmp_path/'state','/tmp/example.sock')
    body=json.loads(settings.read_text())
    assert body['model']=='user-choice'
    assert len(body['hooks']['Stop'])==2
    assert len(body['hooks']['UserPromptSubmit'])==1
    assert settings.with_suffix('.json.before-daikibo').exists()


def test_hook_transports_raw_prompt_and_does_not_ack_inbox(full,full_project,monkeypatch):
    monkeypatch.delenv('DAIKIBO_MANAGED_RUN',raising=False)
    c=full;attach(c,full_project);client=LocalClient(c)
    notice=c.g.inbox(full_project[0],'important','native-test',{'why':'needs review'},'warning')
    payload={'hook_event_name':'UserPromptSubmit','session_id':'claude-live-shape','cwd':str(full_project[3]),'prompt':'続けてください','event_id':'event-one'}
    output=dispatch(client,payload)
    context=json.loads(output['hookSpecificOutput']['additionalContext'])
    assert context['user_source'] and context['project']==full_project[0]
    assert c.s.one("SELECT status FROM inbox WHERE ref='native-test'")['status']=='open'
    again=dispatch(client,payload)
    assert json.loads(again['hookSpecificOutput']['additionalContext'])['user_source']==context['user_source']
    assert dispatch(client,{'hook_event_name':'Stop','session_id':'claude-live-shape','cwd':str(full_project[3]),'stop_hook_active':False})=={}
    monkeypatch.setenv('DAIKIBO_MANAGED_RUN','1')
    assert dispatch(client,payload)=={}


def test_hooks_do_not_enroll_unrelated_workspace(full,tmp_path,monkeypatch):
    monkeypatch.delenv('DAIKIBO_MANAGED_RUN',raising=False)
    assert dispatch(LocalClient(full),{'hook_event_name':'UserPromptSubmit','session_id':'x','cwd':str(tmp_path),'prompt':'ordinary question'})=={}


@pytest.mark.parametrize('kind',['claude','codex'])
def test_native_adapter_argv_has_no_nested_sandbox_or_gateway(full,tmp_path,kind):
    ad={'kind':kind,'executable':kind,'extra_args':[],'model':None}
    argv,_=full.rt.adapters.command(ad,'spec',tmp_path,tmp_path)
    assert '--bare' not in argv and '--sandbox' not in argv
    if kind=='claude':
        assert argv[argv.index('--permission-mode')+1]=='bypassPermissions'
    else:assert '--dangerously-bypass-approvals-and-sandbox' in argv
    assert not any('daikibo.base_url' in x for x in argv)
    assert full.providers.environment(ad)=={}


def test_explicit_provider_is_direct_and_backup_restores(full,full_project,tmp_path):
    from daikibo.operations import restore_backup
    c=full;secret='test-value-not-real-secret'
    c.providers.configure(c.owner,'local-test','anthropic','http://localhost:1234',secret)
    env=c.providers.environment({'kind':'claude','provider':'local-test'})
    assert env['ANTHROPIC_API_KEY']==secret
    assert secret not in canonical(c.providers.list(c.owner)).decode()
    backup=c.ops.backup(c.owner);target=tmp_path/'restored'
    restore_backup(backup['path'],target,backup['sha256'])
    restored=Control(target,mode='validation',start_workers=False)
    try:
        assert restored.providers.environment({'kind':'claude','provider':'local-test'})['ANTHROPIC_API_KEY']==secret
    finally:restored.close()
