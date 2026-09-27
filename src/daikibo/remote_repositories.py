"""GitHub/GitLab wire adapters. They observe requests; they never certify software.

Only standard-library HTTP/Git plumbing is used. Publish retries first reconcile
remote state. No force-push, merge, deployment, or blind POST retry is performed.
"""
from __future__ import annotations

import base64
import http.client
import re
import urllib.parse
from pathlib import Path

from .common import Fault, canonical, need, obj, parse_json, text
from .gitops import git


def normalized_target(body):
    obj(body, required=('kind', 'owner', 'repository', 'base_branch'),
        optional=('api_origin', 'git_origin'))
    kind = body['kind']
    need(kind in {'github', 'gitlab'}, 'unsupported_remote', 'Use github or gitlab')
    segments = body['owner'].split('/') if isinstance(body['owner'], str) else []
    need(segments and (kind == 'gitlab' or len(segments) == 1), 'invalid_remote', 'Invalid namespace')
    for item in [*segments, body['repository']]:
        need(isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', item)
             and item not in {'.', '..'}, 'invalid_remote', 'Invalid namespace/repository')
    branch = body['base_branch']
    # Validate the subset we emit, not arbitrary Git shell input.
    need(isinstance(branch, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', branch)
         and not branch.startswith(('/', '-')) and not branch.endswith(('/', '.'))
         and not any(part in {'', '.', '..'} or part.startswith('.') or part.endswith('.lock') for part in branch.split('/'))
         and '..' not in branch, 'invalid_remote', 'Invalid target branch')
    api = body.get('api_origin', 'https://api.github.com' if kind == 'github' else 'https://gitlab.com')
    origin = body.get('git_origin', 'https://github.com' if kind == 'github' else api)
    for value in (api, origin):
        need(isinstance(value, str), 'invalid_remote', 'HTTPS origin required')
        parsed = urllib.parse.urlsplit(value)
        need(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
             and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment,
             'invalid_remote', 'Use an HTTPS origin without a path/query/credentials')
    return {**body, 'api_origin': api.rstrip('/'), 'git_origin': origin.rstrip('/')}


class RepositoryAPI:
    """One configured repository on REST v3 (GitHub) or v4 (GitLab)."""

    def __init__(self, target, credential, http):
        self.target, self.credential, self.http = target, credential, http
        self.kind = target['kind']
        self.full_name = target['owner'] + '/' + target['repository']
        self.prefix = ('/repos/' + self.full_name if self.kind == 'github'
                       else '/api/v4/projects/' + urllib.parse.quote(self.full_name, safe=''))
        self.project_id = None

    def request(self, path, method='GET', body=None, allow_missing=False):
        headers = {'Accept': 'application/json'}
        if self.kind == 'github':
            headers.update({'Authorization': 'Bearer ' + self.credential,
                            'X-GitHub-Api-Version': '2022-11-28'})
        else:
            headers['PRIVATE-TOKEN'] = self.credential
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            response = self.http(self.target['api_origin'] + self.prefix + path, method,
                                 canonical(body) if body is not None else None, headers,
                                 allowed_origin=self.target['api_origin'])
        except (OSError, http.client.HTTPException) as exc:
            raise Fault('remote_observation_pending',
                        'Remote outcome is unknown. Reconcile before any retry.',
                        {'provider': self.kind, 'method': method, 'reason': type(exc).__name__}) from exc
        status = response['status']
        if status == 404 and allow_missing:
            return None
        need(status in ({200} if method == 'GET' else {200, 201}), 'remote_error',
             'Remote did not confirm the operation', {'provider': self.kind, 'status': status, 'method': method})
        try:
            return parse_json(response['body'])
        except Fault as exc:
            raise Fault('remote_invalid_response', 'Remote returned invalid JSON; outcome needs reconciliation') from exc

    def identity(self):
        value = self.request('')
        need(isinstance(value, dict), 'remote_invalid_response', 'Repository response must be an object')
        name = value.get('full_name' if self.kind == 'github' else 'path_with_namespace')
        need(isinstance(name, str) and name.casefold() == self.full_name.casefold(),
             'remote_conflict', 'Configured repository does not match observed repository')
        ident = value.get('id')
        need(type(ident) is int and ident > 0, 'remote_invalid_response', 'Repository ID is missing')
        self.project_id = ident
        return ident

    def branch(self, name):
        path = ('/git/ref/heads/' if self.kind == 'github' else '/repository/branches/')
        value = self.request(path + urllib.parse.quote(name, safe=''), allow_missing=True)
        if value is None:
            return None
        need(isinstance(value, dict), 'remote_invalid_response', 'Branch response is not an object')
        commit = value.get('object', {}).get('sha') if self.kind == 'github' else value.get('commit', {}).get('id')
        need(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40,64}', commit),
             'remote_invalid_response', 'Branch commit is missing')
        return commit

    def push(self, commit, branch):
        remote = self.target['git_origin'] + '/' + self.full_name + '.git'
        username = 'x-access-token' if self.kind == 'github' else 'oauth2'
        encoded = base64.b64encode((username + ':' + self.credential).encode()).decode()
        # Same-user process, not a security boundary. Avoid accidental token disclosure in argv/logs.
        env = {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'http.' + self.target['git_origin'] + '/.extraHeader',
               'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + encoded,
               'GIT_CONFIG_KEY_1': 'http.followRedirects', 'GIT_CONFIG_VALUE_1': 'false',
               'GIT_TERMINAL_PROMPT': '0'}
        git(Path(commit['git_dir']), 'push', remote, commit['commit'] + ':refs/heads/' + branch,
            env_extra=env, timeout=300)

    def requests(self, branch):
        endpoint = '/pulls' if self.kind == 'github' else '/merge_requests'
        params = ({'state': 'all', 'head': self.target['owner'] + ':' + branch,
                   'base': self.target['base_branch']} if self.kind == 'github'
                  else {'state': 'all', 'source_branch': branch, 'target_branch': self.target['base_branch']})
        result, seen = [], set()
        for page in range(1, 101):
            query = urllib.parse.urlencode({**params, 'per_page': 100, 'page': page})
            values = self.request(endpoint + '?' + query)
            need(isinstance(values, list) and len(values) <= 100,
                 'remote_invalid_response', 'Invalid request page')
            for value in values:
                need(isinstance(value, dict), 'remote_invalid_response', 'Invalid request item')
                ident = value.get('number' if self.kind == 'github' else 'iid')
                need(type(ident) is int and ident > 0 and ident not in seen,
                     'remote_invalid_response', 'Missing or duplicate request identity across pages')
                seen.add(ident)
                result.append(value)
            if len(values) < 100:
                return result
        raise Fault('remote_list_incomplete', 'Request listing exceeded page limit; no write is safe')

    def number(self, value):
        ident = value.get('number' if self.kind == 'github' else 'iid') if isinstance(value, dict) else None
        need(type(ident) is int and ident > 0, 'remote_invalid_response', 'Missing request number')
        return ident

    def create(self, branch, title, description):
        if self.kind == 'github':
            return self.request('/pulls', 'POST', {'title': title, 'body': description, 'head': branch,
                                                 'base': self.target['base_branch']})
        return self.request('/merge_requests', 'POST', {'title': title, 'description': description,
                            'source_branch': branch, 'target_branch': self.target['base_branch'],
                            'remove_source_branch': False, 'squash': False})

    def detail(self, number):
        return self.request(('/pulls/' if self.kind == 'github' else '/merge_requests/') + str(number))

    def observe(self, value, branch, commit, marker):
        need(isinstance(value, dict), 'remote_invalid_response', 'Request detail must be an object')
        number = self.number(value)
        if self.kind == 'github':
            head, base = value.get('head') or {}, value.get('base') or {}
            state, text_body = value.get('state'), value.get('body') or ''
            correct = (state == 'open' and not value.get('merged') and head.get('ref') == branch
                       and head.get('sha') == commit and base.get('ref') == self.target['base_branch']
                       and (head.get('repo') or {}).get('id') == self.project_id
                       and (base.get('repo') or {}).get('id') == self.project_id)
            url = value.get('html_url')
        else:
            state, text_body = value.get('state'), value.get('description') or ''
            correct = (state == 'opened' and value.get('source_branch') == branch
                       and value.get('target_branch') == self.target['base_branch']
                       and value.get('sha') == commit
                       and value.get('source_project_id') == self.project_id
                       and value.get('target_project_id') == self.project_id)
            url = value.get('web_url')
        need(correct and isinstance(text_body, str) and marker in text_body,
             'remote_conflict', 'Observed request differs, is closed/merged, or lacks the delivery marker')
        need(isinstance(url, str) and urllib.parse.urlsplit(url).scheme == 'https',
             'remote_invalid_response', 'Request URL is missing')
        return {'number': number, 'url': url, 'kind': self.kind, 'state': state,
                'source_project_id': self.project_id, 'observed_commit': commit}
