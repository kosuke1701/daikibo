"""Real CLI connections retain their control home across clients and restarts."""
import json
import os
from pathlib import Path
import subprocess
import sys

import daikibo
import pytest


@pytest.mark.parametrize('selected,locations', [
    ('codex', {'codex': '.agents'}),
    ('claude', {'claude': '.claude'}),
    ('both', {'codex': '.agents', 'claude': '.claude'}),
])
def test_connect_client_and_resume_custom_home(tmp_path, selected, locations):
    workspace = tmp_path / 'project with spaces'; workspace.mkdir()
    home = tmp_path / 'custom state'
    env = {**os.environ, 'PYTHONPATH': str(Path(daikibo.__file__).resolve().parent.parent)}
    prefix = [sys.executable, '-m', 'daikibo', '--home', 'custom state']
    def run(argv):
        result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)
    try:
        connected = run(prefix + ['connect', '--client', selected, '--workspace', str(workspace)])
        assert set(connected['skills']) == set(locations)
        for client, directory in locations.items():
            skill = workspace / directory / 'skills' / 'daikibo_dev'
            binding = json.loads((skill / 'references/connection.json').read_text())
            assert binding['home'] == str(home)
            assert binding['project'] == connected['project']
            assert binding['workspace'] == str(workspace)
            assert (skill / 'SKILL.md').is_file()
        assert (workspace / '.claude/settings.local.json').exists() == ('claude' in locations)
        command = binding['command']
        params = {'session': binding['session'], 'turn_id': 'one', 'content': '保持する原文'}
        first = run(command + ['call', 'native.input', '--json', json.dumps(params)])
        run(command + ['stop'])
        run(command + ['start'])
        repeated = run(command + ['call', 'native.input', '--json', json.dumps(params)])
        assert repeated['replayed'] and repeated['source'] == first['source']
        raw_context = subprocess.run(command + ['call', 'native.context', '--json', json.dumps({'session': binding['session']})], cwd='/tmp', env=env, capture_output=True, text=True, timeout=20)
        assert raw_context.returncode == 0, raw_context.stdout + raw_context.stderr
        context = json.loads(raw_context.stdout)
        assert context['project'] == connected['project']
        assert context['last_source'] == first['source']
    finally:
        subprocess.run(prefix + ['stop'], cwd=tmp_path, env=env, capture_output=True, timeout=20)
