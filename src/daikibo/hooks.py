"""Claude Code hook adapter; no LLM judgment or security boundary lives here."""
from __future__ import annotations
import os
import shlex
import sys
from pathlib import Path
from .common import Fault, atomic_write, canonical, digest, need, parse_json, uid

EVENTS = ('SessionStart', 'UserPromptSubmit', 'Stop')


def install(workspace, home, socket_path, python=None):
    """Merge only our hook entries; preserve all unrelated user settings."""
    workspace = Path(workspace).resolve(); need(workspace.is_dir(), 'workspace_missing', 'No workspace')
    target = workspace / '.claude' / 'settings.local.json'
    settings = parse_json(target.read_bytes()) if target.exists() else {}
    need(isinstance(settings, dict), 'invalid_settings', 'Claude settings must be an object')
    bridge = target.parent / 'daikibo-bridge.json'
    previous = parse_json(bridge.read_bytes()) if bridge.exists() else {}
    hooks = settings.setdefault('hooks', {})
    need(isinstance(hooks, dict), 'invalid_settings', 'hooks must be an object')
    command = shlex.join([python or sys.executable, '-m', 'daikibo', '--home', str(home),
                          '--socket', str(socket_path), 'hook'])
    old_command = previous.get('command')
    if old_command and old_command != command:
        _remove_command(hooks, old_command)
    installed = []
    for event in EVENTS:
        entries = hooks.setdefault(event, [])
        need(isinstance(entries, list), 'invalid_settings', 'Event entries must be arrays')
        duplicate = any(any(h.get('command') == command for h in e.get('hooks', [])) for e in entries)
        if not duplicate:
            entries.append({'hooks': [{'type':'command', 'command':command, 'timeout':10}]})
        installed.append(event)
    if target.exists() and not target.with_suffix('.json.before-daikibo').exists():
        atomic_write(target.with_suffix('.json.before-daikibo'), target.read_bytes())
    atomic_write(target, canonical(settings))
    atomic_write(bridge,canonical({'format':'daikibo.hooks.v1','command':command,'events':list(EVENTS),
                                   'home':str(home),'socket':str(socket_path)}))
    return {'settings':str(target), 'events':installed, 'unrelated_settings_preserved':True,
            'restart_note':'Claude Code may need to reload project hooks; live CLI integration remains to be verified.'}


def _remove_command(hooks, command):
    """Remove exactly our recorded command, preserving unrelated later changes."""
    for event in EVENTS:
        entries = hooks.get(event, [])
        remaining = []
        for entry in entries:
            if not isinstance(entry,dict) or not isinstance(entry.get('hooks'),list):
                remaining.append(entry); continue
            keep = [h for h in entry['hooks'] if h.get('command') != command]
            if keep: remaining.append({**entry, 'hooks':keep})
        if remaining: hooks[event] = remaining
        else: hooks.pop(event,None)


def uninstall(workspace):
    workspace=Path(workspace).resolve()
    target=workspace/'.claude/settings.local.json'; bridge=target.parent/'daikibo-bridge.json'
    if not bridge.exists(): return {'removed':False,'reason':'No managed hook manifest','history_preserved':True}
    manifest=parse_json(bridge.read_bytes())
    if target.exists():
        settings=parse_json(target.read_bytes())
        _remove_command(settings.get('hooks',{}),manifest['command'])
        atomic_write(target,canonical(settings))
    bridge.unlink()
    return {'removed':True,'history_preserved':True,'skill_preserved':True,'controller_stopped':False}


def dispatch(client, payload):
    """Return hook-protocol JSON. Never claim that a hook is a review run."""
    if os.environ.get('DAIKIBO_MANAGED_RUN') == '1': return {}
    need(isinstance(payload, dict), 'invalid_hook', 'Expected hook object')
    event = payload.get('hook_event_name')
    if event not in EVENTS: return {}
    cwd, session = payload.get('cwd'), payload.get('session_id')
    if not cwd or not session: return {}
    location = client.call('native.lookup', {'cwd':cwd})
    # Installing hooks doesn't enroll every future conversation automatically.
    if not location['attached']: return {}
    client.call('native.attach', {'session':session, 'cwd':cwd, 'project':location['project'], 'register_repository':False})
    if event == 'UserPromptSubmit':
        prompt = payload.get('prompt', '')
        if not prompt: return {}
        transcript = Path(payload['transcript_path']) if payload.get('transcript_path') else None
        # The transcript offset distinguishes repeated equal prompts across turns.
        # Without an offset/event id, use a new ID instead of dropping repeated input.
        event_id = payload.get('event_id')
        if not event_id and transcript and transcript.is_file():
            st = transcript.stat(); event_id = digest({'session':session,'prompt':prompt,'offset':st.st_size,'modified':st.st_mtime_ns})
        result = client.call('native.input', {'session':session,'content':prompt,'turn_id':event_id or uid('TURN'),'origin':'claude-hook'})
        hint = {'session':session,'project':result['project'],'user_source':result['source'],
                'pending':result.get('notifications',[])[:20], 'rule':'Use daikibo operations for this project. Record actual jobs/reviews. Do not use a chat statement as completion. A new user message may require conflict analysis.'}
        return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':canonical(hint).decode()}}
    if event == 'Stop':
        return client.call('native.stop_feedback', {'session':session,'stop_hook_active':bool(payload.get('stop_hook_active'))})
    context = client.call('native.context', {'session':session})
    return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':canonical(context).decode()}}
