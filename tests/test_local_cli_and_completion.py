"""Real CLI/daemon and error-path tests without a Claude/Codex executable."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from daikibo.common import Fault, parse_json, digest
from daikibo.rpc import Client
from conftest import make_task, finish_task


def cli_env():
    env=dict(os.environ)
    if env.get('DAIKIBO_TEST_INSTALLED')!='1':
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    env.pop('DAIKIBO_MANAGED_RUN',None)
    return env


def test_actual_cli_connect_hooks_restart_and_tokenless_calls(tmp_path):
    workspace=tmp_path/'project with spaces';workspace.mkdir()
    home=tmp_path/'state';socket=tmp_path/'control.sock'
    command=[sys.executable,'-m','daikibo','--home',str(home),'--socket',str(socket)]
    env=cli_env()
    def call(*args,payload=None):
        result=subprocess.run(command+list(args),input=json.dumps(payload) if payload is not None else None,
                              text=True,capture_output=True,env=env,timeout=20)
        assert result.returncode==0,(result.stdout,result.stderr)
        return json.loads(result.stdout)
    try:
        initial=call('connect','--workspace',str(workspace),'--name','CLI project')
        project=initial['project']
        assert (workspace/'.claude/skills/daikibo_dev/SKILL.md').is_file()
        assert initial['hooks']['unrelated_settings_preserved']
        assert not (home/'owner.token').exists() and not (home/'keys.json').exists()
        assert call('doctor')['execution_model']=='cooperative-single-user'
        equals=subprocess.run([sys.executable,'-m','daikibo','--home='+str(home),'--socket='+str(socket),'doctor'],
                              capture_output=True,text=True,env=env,timeout=10)
        assert equals.returncode==0 and json.loads(equals.stdout)['control_home']==str(home)
        conflict=subprocess.run([sys.executable,'-m','daikibo','--home',str(tmp_path/'other-state'),
                    '--socket',str(socket),'start'],capture_output=True,text=True,env=env,timeout=10)
        assert conflict.returncode==2 and json.loads(conflict.stdout)['error']['code']=='wrong_controller'
        second=call('connect','--workspace',str(workspace))
        assert second['project']==project
        settings=json.loads((workspace/'.claude/settings.local.json').read_text())
        assert len(settings['hooks']['UserPromptSubmit'])==1
        hook=call('hook',payload={'hook_event_name':'UserPromptSubmit','session_id':'real-cli-session',
             'cwd':str(workspace),'prompt':'仕様の相談から始めます。','event_id':'real-turn-one'})
        context=json.loads(hook['hookSpecificOutput']['additionalContext'])
        assert context['project']==project
        original=call('call','source.read','--json',json.dumps({'source':context['user_source']}))
        assert original['content']=='仕様の相談から始めます。'
        repeated=call('hook',payload={'hook_event_name':'UserPromptSubmit','session_id':'real-cli-session',
             'cwd':str(workspace),'prompt':'仕様の相談から始めます。','event_id':'real-turn-one'})
        assert json.loads(repeated['hookSpecificOutput']['additionalContext'])['user_source']==context['user_source']
        call('stop')
        for _ in range(100):
            if not socket.exists():break
            time.sleep(.03)
        restarted=call('start');assert restarted['running']
        saved=call('call','native.context','--json',json.dumps({'session':'real-cli-session'}))
        assert saved['last_source']==context['user_source']
        stop=call('hook',payload={'hook_event_name':'Stop','session_id':'real-cli-session',
                'cwd':str(workspace),'stop_hook_active':False})
        assert stop=={} # Stopping specification discussion is not prohibited.
    finally:
        try:call('stop')
        except (AssertionError,subprocess.TimeoutExpired):pass


def test_hook_on_missing_controller_does_not_claim_success(tmp_path):
    command=[sys.executable,'-m','daikibo','--home',str(tmp_path/'state'),'--socket',str(tmp_path/'missing.sock'),'hook']
    r=subprocess.run(command,input=json.dumps({'hook_event_name':'SessionStart','session_id':'s','cwd':str(tmp_path)}),
                     capture_output=True,text=True,timeout=5,env=cli_env())
    assert r.returncode==0
    body=json.loads(r.stdout)
    assert 'No completion was certified' in body['systemMessage']
    assert 'decision' not in body


def test_native_completed_task_rechecks_missing_evidence_without_waiting_for_sweep(full,full_project):
    c=full;session='source-session'
    c.native.attach(c.owner,session,str(full_project[3]),project=full_project[0])
    task=make_task(c,full_project);finish_task(c,full_project[0],task)
    assert c.native.completion(c.owner,session,task)['completed']
    row=c.s.one("SELECT id FROM receipts WHERE subject=? AND role='test:unit'",(task,),True)
    blob=c.g.receipt(row['id'])['result']['report_blob'];(c.s.blobs/blob[:2]/blob[2:]).unlink()
    result=c.native.completion(c.owner,session,task)
    assert not result['completed'] and result['blockers']
    assert c.w.task(c.owner,task)['status']=='completed' # history is not silently erased


def test_cancelled_queued_job_never_dispatches_even_with_stale_scheduler_row(full,full_project,monkeypatch):
    c=full;p=full_project[0]
    job=c.jobs.submit(c.owner,'index',{'repo':full_project[1]})
    stale=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True)
    c.jobs.cancel(c.owner,job['id'],'User withdrew request')
    def forbidden(*a,**kw):raise AssertionError('Cancelled operation was executed')
    monkeypatch.setattr(c.idx,'index',forbidden)
    result=c.jobs.run_one(stale)
    assert result['status']=='cancelled' and result['replayed']


def test_terminal_job_replay_does_not_repeat_external_work(full,full_project,monkeypatch):
    c=full
    job=c.jobs.submit(c.owner,'index',{'repo':full_project[1]})
    row=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True)
    first=c.jobs.run_one(row);assert first['status']=='succeeded'
    def forbidden(*a,**kw):raise AssertionError('Terminal job was repeated')
    monkeypatch.setattr(c.idx,'index',forbidden)
    repeated=c.jobs.run_one(row)
    assert repeated['replayed'] and repeated['result']==first['result']


def test_skill_upgrade_preserves_previous_copy_without_root(tmp_path):
    dest=tmp_path/'skill';dest.mkdir();(dest/'SKILL.md').write_text('previous content')
    args=[sys.executable,'-m','daikibo','install-skill','--destination',str(dest)]
    denied=subprocess.run(args,capture_output=True,text=True,timeout=5,env=cli_env())
    assert denied.returncode==2 and (dest/'SKILL.md').read_text()=='previous content'
    upgraded=subprocess.run(args+['--replace'],capture_output=True,text=True,timeout=5,env=cli_env())
    assert upgraded.returncode==0
    record=json.loads(upgraded.stdout)
    assert (Path(record['backup'])/'SKILL.md').read_text()=='previous content'
    assert 'daikibo_dev' in (dest/'SKILL.md').read_text()


def test_hook_upgrade_and_uninstall_preserve_unrelated_user_changes(tmp_path):
    from daikibo.hooks import install,uninstall
    install(tmp_path,tmp_path/'one','/tmp/one.sock',python='/old/venv/python')
    settings=tmp_path/'.claude/settings.local.json'
    body=json.loads(settings.read_text());body['model']='other-setting'
    body['hooks']['UserPromptSubmit'].append({'hooks':[{'type':'command','command':'user-command'}]})
    settings.write_text(json.dumps(body))
    install(tmp_path,tmp_path/'two','/tmp/two.sock',python='/new/venv/python')
    body=json.loads(settings.read_text());commands=[h['command'] for e in body['hooks']['UserPromptSubmit'] for h in e['hooks']]
    assert len(commands)==2 and not any('/old/venv/' in x for x in commands)
    assert any('/new/venv/' in x for x in commands) and 'user-command' in commands
    result=uninstall(tmp_path);assert result['removed'] and result['history_preserved']
    body=json.loads(settings.read_text())
    assert body['model']=='other-setting' and list(body['hooks'])==['UserPromptSubmit']
    assert body['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']=='user-command'
    assert not uninstall(tmp_path)['removed']


def test_32_real_same_user_processes_have_separate_observed_runs(full,full_project):
    import concurrent.futures
    c=full;p=full_project[0];snapshot=c.sn.capture(c.owner,p)
    script='import os;print(os.geteuid())'
    def run(i):
        receipt,_,_=c.rt.observe(p,None,'parallel-'+str(i),'probe',None,digest({'i':i}),snapshot,
             lambda w,h,d:([sys.executable,'-c',script],None),timeout=10)
        return receipt
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results=list(pool.map(run,range(32)))
    assert len({r['run'] for r in results})==32
    assert all(r['exit_code']==0 and not r['timed_out'] for r in results)
    assert {c.s.blob_get(r['stdout_blob']).decode().strip() for r in results}=={str(os.geteuid())}
    assert not c.rt.active and not list(c.rt.workroot.iterdir())


def test_cancel_after_scheduler_claim_before_execution_records_terminal_state(full,full_project,monkeypatch):
    c=full;job=c.jobs.submit(c.owner,'index',{'repo':full_project[1]})
    c.s.execute("UPDATE jobs SET status='running' WHERE id=?",(job['id'],))
    stale=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True)
    c.jobs.cancel(c.owner,job['id'],'Cancel before dispatch')
    def forbidden(*a,**kw):raise AssertionError('Cancelled pre-start work was executed')
    monkeypatch.setattr(c.idx,'index',forbidden)
    assert c.jobs.run_one(stale)['status']=='cancelled'
    row=c.jobs.get(c.owner,job['id'])
    assert row['status']=='cancelled' and row['ended'] is not None


def test_cancel_completed_job_does_not_relabel_or_undo_completion(full,full_project):
    c=full;job=c.jobs.submit(c.owner,'index',{'repo':full_project[1]})
    c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],),True))
    result=c.jobs.cancel(c.owner,job['id'],'Withdraw already-finished action')
    assert not result['cancel_requested'] and result['status']=='succeeded'
    assert c.jobs.get(c.owner,job['id'])['status']=='succeeded'
