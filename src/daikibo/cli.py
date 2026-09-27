"""Local CLI and conversation bridge. No token or privileged operator session required."""
from __future__ import annotations
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from .common import Fault,canonical,need,parse_json
from .rpc import Client


def print_json(value):print(json.dumps(value,ensure_ascii=False,indent=2))
def load_json(value):
    if value=='-':return parse_json(sys.stdin.buffer.read())
    if value.startswith('@'):return parse_json(Path(value[1:]).read_bytes())
    return parse_json(value)

def client(args,owner=False):
    return Client(args.socket, timeout=args.timeout)


def wait(c,job):
    if os.environ.get('DAIKIBO_MANAGED_RUN')=='1' and os.environ.get('DAIKIBO_JOB_ID') == job:
        raise Fault('self_wait',
                    'This worker is waiting for its own managed job; return the assigned result so the outer manager can collect the receipt and candidate.',
                    {'job':job,'run':os.environ.get('DAIKIBO_RUN_ID'),
                     'task':os.environ.get('DAIKIBO_TASK_ID'),'role':os.environ.get('DAIKIBO_RUN_ROLE')})
    while True:
        row=c.call('job.get',{'job':job})
        if row['status'] not in {'queued','running','retry_wait'}:return row
        time.sleep(0.5)


def chat(args):
    c=client(args,owner=True);project=args.project
    print('daikibo_dev の管理された対話です。仕様の相談を入力してください。終了は :quit。')
    print('普段のClaude Codeからも同じ対話記録を利用できます。裁定は提案の版と発言に紐付けます。')
    if project:print_json(c.call('inbox.get',{'project':project}))
    while True:
        try:line=input('あなた > ').strip()
        except (EOFError,KeyboardInterrupt):break
        if not line:continue
        if line==':quit':break
        try:
            if line==':status':
                need(project,'project_required','まだプロジェクトがありません。');print_json(c.call('workflow.status',{'project':project}));continue
            if line==':inbox':
                need(project,'project_required','まだプロジェクトがありません。');print_json(c.call('inbox.get',{'project':project}));continue
            if line.startswith(':decide '):
                _,decision=line.split(maxsplit=1);row=c.call('decision.get',{'decision':decision});print_json(row)
                print('表示した提案の選択肢を入力してください。保留は defer、却下は reject です。')
                choice=input('裁定 > ').strip();utterance=input('判断理由 > ').strip()
                print_json(c.call('decision.respond',{'decision':decision,'expected_digest':row['digest'],'choice':choice,'utterance':utterance or choice}));continue
            if line.startswith(':ack '):
                item=line.split(maxsplit=1)[1];utterance=input('確認内容 > ').strip()
                print_json(c.call('inbox.acknowledge',{'item':item,'utterance':utterance}));continue
            if line in {':pause',':resume'}:
                print_json(c.call('workflow.pause',{'project':project,'paused':line==':pause'}));continue
            intake=c.call('dialogue.input',{'content':line,'project':project,'name':args.name})
            project=intake['project'];print('project:',project)
            job=c.call('job.submit',{'kind':'supervisor.turn','args':{'project':project,'adapter':args.adapter,'message':line,'source':intake['source']['id']}})
            result=wait(c,job['id'])
            if result['status']=='succeeded':
                output=result['result'];print('daikibo_dev >',output.get('message',''));print_json(output.get('actions',[]))
            else:print_json(result)
            print_json(c.call('inbox.get',{'project':project}))
        except (Fault,OSError) as exc:
            print_json({'error':exc.as_dict() if isinstance(exc,Fault) else str(exc)})


def start_controller(args):
    """Start a normal same-user daemon. Return only after the socket is responsive."""
    import subprocess
    c = client(args)
    try:
        state=c.call('system.doctor')
        need(state.get('control_home')==str(Path(args.home).resolve()),'wrong_controller',
             'This socket belongs to a different state directory; use a separate socket')
        return {'running':True,'already_running':True,'socket':args.socket}
    except Fault as exc:
        if exc.code != 'control_unavailable': raise
    home = Path(args.home); home.mkdir(parents=True, exist_ok=True)
    logfile = home / 'daemon.log'
    with logfile.open('ab') as log:
        process = subprocess.Popen([sys.executable,'-m','daikibo','--home',str(home),'--socket',args.socket,'serve'],
                                   stdout=log,stderr=log,stdin=subprocess.DEVNULL,start_new_session=True)
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise Fault('start_failed','Controller exited during startup',logfile.read_text(errors='replace')[-5000:])
        try:
            c.call('api.describe', {'method':'system.doctor'})
            return {'running':True,'pid':process.pid,'socket':args.socket,'log':str(logfile)}
        except Fault as exc:
            if exc.code != 'control_unavailable': raise
            time.sleep(0.1)
    process.terminate(); process.wait(timeout=5)
    raise Fault('start_timeout','Controller did not become ready; inspect daemon.log')


def stop_controller(args):
    """Wait for the controller to release its state before allowing a restart."""
    import fcntl
    c = client(args)
    state = c.call('system.doctor')
    home = Path(args.home).resolve()
    need(state.get('control_home') == str(home), 'wrong_controller',
         'This socket belongs to a different state directory')
    result = c.call('system.shutdown')
    deadline = time.monotonic() + args.timeout
    with (home / 'owner.lock').open('rb') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Fault('stop_timeout', 'Shutdown requested but controller has not finished; inspect daemon.log')
                time.sleep(0.05)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return {**result, 'stopped': True}


def main(argv=None):
    parser=argparse.ArgumentParser(prog='daikibo',description='Evidence-governed large-scale development control plane')
    parser.add_argument('--home',default=os.environ.get('DAIKIBO_HOME',str(Path.home()/'.local/state/daikibo')))
    parser.add_argument('--socket',default=os.environ.get('DAIKIBO_SOCKET'))
    parser.add_argument('--token-file',default=os.environ.get('DAIKIBO_TOKEN_FILE'))
    parser.add_argument('--timeout',type=float,default=60)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('version')
    sub.add_parser('start'); sub.add_parser('stop'); sub.add_parser('doctor')
    sub.add_parser('hook')
    disconnect=sub.add_parser('disconnect');disconnect.add_argument('--workspace',default=os.getcwd())
    connect=sub.add_parser('connect');connect.add_argument('--workspace',default=os.getcwd());connect.add_argument('--name',default='New project');connect.add_argument('--project')
    connect.add_argument('--client',choices=['claude','codex','both'],default='claude')
    connect.add_argument('--no-hooks',action='store_true')
    init=sub.add_parser('init');init.add_argument('--mode',choices=['governed','validation'],default='governed')
    serve=sub.add_parser('serve');serve.add_argument('--mode',choices=['governed','validation'],default=None)
    for command in ('call','owner'):
        p=sub.add_parser(command);p.add_argument('method');p.add_argument('--json',default='{}');p.add_argument('--request-id')
    p=sub.add_parser('wait');p.add_argument('job')
    p=sub.add_parser('chat');p.add_argument('--adapter',required=True);p.add_argument('--project');p.add_argument('--name',default='New project')
    p=sub.add_parser('restore');p.add_argument('archive');p.add_argument('--sha256',required=True)
    p=sub.add_parser('inspect-baseline');p.add_argument('archive');p.add_argument('--sha256',required=True)
    p=sub.add_parser('install-skill');p.add_argument('--destination',default=str(Path.home()/'.claude/skills/daikibo_dev'));p.add_argument('--replace',action='store_true')
    args=parser.parse_args(argv)
    args.home=str(Path(args.home).expanduser().resolve())
    if args.socket is not None:
        args.socket=str(Path(args.socket).expanduser().resolve())
    # A short per-home socket avoids collisions between two normal local projects.
    if args.socket is None:
        from .common import digest
        args.socket='/tmp/daikibo-'+digest(str(Path(args.home).resolve()).encode())[:18]+'.sock'
    try:
        if args.command=='start':
            print_json(start_controller(args));return 0
        if args.command=='stop':
            print_json(stop_controller(args));return 0
        if args.command=='doctor':
            print_json(client(args).call('system.doctor'));return 0
        if args.command=='hook':
            from .hooks import dispatch
            try:
                result=dispatch(client(args), parse_json(sys.stdin.buffer.read()))
            except Fault as exc:
                # Do not forge a PASS or erase the user prompt on connection failure.
                result={'systemMessage':'daikibo_dev workflow support unavailable: '+exc.message+'. No completion was certified.'}
            print_json(result);return 0
        if args.command=='disconnect':
            from .hooks import uninstall
            print_json(uninstall(args.workspace));return 0
        if args.command=='connect':
            import shutil
            from .common import atomic_write, digest
            from .hooks import install
            start_controller(args)
            workspace=Path(args.workspace).resolve()
            result=client(args).call('native.attach',{'session':'workspace:'+digest(str(workspace).encode())[:24],
                                     'cwd':str(workspace),'project':args.project,'name':args.name,'client':args.client})
            source=Path(__file__).with_name('assets')/'skill'
            clients=['claude','codex'] if args.client=='both' else [args.client]
            connection={'format':'daikibo.connection.v1','workspace':str(workspace),
                        'home':str(Path(args.home).resolve()),'socket':args.socket,
                        'session':result['session'],'project':result['project'],
                        'command':[sys.executable,'-m','daikibo','--home',str(Path(args.home).resolve()),'--socket',args.socket]}
            result['skills']={}
            for selected in clients:
                destination=workspace/('.claude' if selected=='claude' else '.agents')/'skills'/'daikibo_dev'
                shutil.copytree(source,destination,dirs_exist_ok=True)
                atomic_write(destination/'references'/'connection.json',canonical(connection))
                result['skills'][selected]=str(destination)
            result['skill']=next(iter(result['skills'].values()))
            result['connection']=connection
            if not args.no_hooks and 'claude' in clients:result['hooks']=install(workspace,args.home,args.socket)
            if 'codex' in clients:
                result['codex_input_mode']='skill-relay'
            print_json(result);return 0
        if args.command=='version':
            from . import __version__;print(__version__);return 0
        if args.command=='inspect-baseline':
            from .knowledge_history import inspect_archive
            print_json(inspect_archive(args.archive,args.sha256));return 0
        if args.command=='restore':
            from .operations import restore_backup
            print_json(restore_backup(args.archive,args.home,args.sha256));return 0
        if args.command=='install-skill':
            import shutil
            source=Path(__file__).with_name('assets')/'skill';dest=Path(args.destination)
            backup=None
            if dest.exists():
                need(args.replace,'destination_exists','Use --replace to upgrade the skill while preserving a backup')
                from .common import uid
                backup=dest.with_name(dest.name+'.before-'+uid('upgrade'))
                dest.rename(backup)
            shutil.copytree(source,dest);print_json({'installed':str(dest),'backup':str(backup) if backup else None,'requires':'Running local controller; no token or extra OS privileges'});return 0
        if args.command in {'init','serve'}:
            from .control import Control
            from .common import atomic_write
            config=Path(args.home)/'deployment.json'
            prior=parse_json(config.read_bytes()) if config.exists() else None
            mode=args.mode or (prior['mode'] if prior else 'governed')
            if prior:need(prior['mode']==mode,'mode_change_requires_migration','Do not relabel an existing validation database as governed')
            control=Control(args.home,mode,start_workers=args.command=='serve')
            atomic_write(config,canonical({'mode':mode,'format':'daikibo.deployment.v1'}))
            if args.command=='init':
                try:print_json({'initialized':args.home,'mode':mode,'execution_model':'cooperative-single-user','doctor':control.ops.doctor(control.sec.authenticate(Path(control.sec.bootstrap()).read_text()))})
                finally:control.close()
                return 0
            from .rpc import Server
            server=Server(control,args.socket)
            def stop(signum,frame):raise KeyboardInterrupt
            signal.signal(signal.SIGTERM,stop)
            print_json({'listening':args.socket,'mode':mode,'home':args.home});sys.stdout.flush()
            try:server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:pass
            finally:server.server_close();control.close()
            return 0
        if args.command=='chat':chat(args);return 0
        c=client(args,owner=args.command=='owner')
        if args.command=='wait':
            result=wait(c,args.job);print_json(result);return 0 if result['status']=='succeeded' else 2
        result=c.call(args.method,load_json(args.json),args.request_id);print_json(result);return 0
    except Fault as exc:print_json({'ok':False,'error':exc.as_dict()});return 2
    except (OSError,ValueError) as exc:print_json({'ok':False,'error':{'code':'local_error','message':str(exc)}});return 2

if __name__=='__main__':raise SystemExit(main())
