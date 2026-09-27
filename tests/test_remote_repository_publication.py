"""Local REST fixtures + real local Git pushes, NOT a live provider acceptance."""
import copy
import json
from pathlib import Path
import subprocess
import urllib.parse

import pytest

from daikibo.common import Fault, canonical, digest, timestamp
from daikibo.remote_repositories import RepositoryAPI, normalized_target
from daikibo.gitops import git
from conftest import make_task, finish_task
from test_delivery_git_and_recovery import profile


class WireRepository:
    def __init__(self, kind, full_name, commit):
        self.kind, self.full_name, self.commit = kind, full_name, commit
        self.project_id = 17
        self.remote_branch = commit
        self.values = []
        self.post_count = 0
        self.requests = []
        self.created = None
        self.lose_post = False
        self.post_hook = None
        self.detail_hook = None
        self.pages = None
        self.token = 'fixture-token-not-real'

    def item(self, branch, target, description, number=1):
        if self.kind == 'github':
            return {'number': number, 'state': 'open', 'merged': False, 'body': description,
                    'head': {'ref': branch, 'sha': self.commit, 'repo': {'id': 17}},
                    'base': {'ref': target, 'repo': {'id': 17}},
                    'html_url': 'https://github.example/group/repo/pull/' + str(number)}
        return {'iid': number, 'state': 'opened', 'description': description,
                'source_branch': branch, 'target_branch': target, 'sha': self.commit,
                'source_project_id': 17, 'target_project_id': 17,
                'web_url': 'https://gitlab.example/group/repo/-/merge_requests/' + str(number)}

    def __call__(self, url, method='GET', body=None, headers=None, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        prefix = '/repos/' + self.full_name if self.kind == 'github' else '/api/v4/projects/' + urllib.parse.quote(self.full_name, safe='')
        assert parsed.path.startswith(prefix)
        path, query = parsed.path[len(prefix):], urllib.parse.parse_qs(parsed.query)
        assert headers.get('Authorization') == 'Bearer ' + self.token if self.kind == 'github' else headers.get('PRIVATE-TOKEN') == self.token
        assert kwargs['allowed_origin'] == 'https://' + parsed.netloc
        payload = json.loads(body) if body else None
        self.requests.append((method, path, query, payload))
        status = 200
        endpoint = '/pulls' if self.kind == 'github' else '/merge_requests'
        if path == '':
            value = {'id': 17, 'full_name' if self.kind == 'github' else 'path_with_namespace': self.full_name}
        elif path.startswith(('/git/ref/heads/', '/repository/branches/')):
            if self.remote_branch is None:
                status, value = 404, {'message': 'missing'}
            else:
                value = {'object': {'sha': self.remote_branch}} if self.kind == 'github' else {'commit': {'id': self.remote_branch}}
        elif path == endpoint and method == 'GET':
            assert query['state'] == ['all']
            value = self.pages.get(int(query['page'][0]), []) if self.pages is not None else self.values
        elif path == endpoint and method == 'POST':
            self.post_count += 1
            self.created = self.item(payload.get('head', payload.get('source_branch')),
                                     payload.get('base', payload.get('target_branch')),
                                     payload.get('body', payload.get('description')))
            self.values = [self.created]
            if self.post_hook:
                self.post_hook(self.created)
            if self.lose_post:
                self.lose_post = False
                raise OSError('Response lost after remote applied POST')
            status, value = 201, self.created
        elif path.startswith(endpoint + '/'):
            ident = int(path.rsplit('/', 1)[1])
            value = next(v for v in self.values if v.get('number', v.get('iid')) == ident)
            if self.detail_hook:
                self.detail_hook(value)
        else:
            raise AssertionError((method, path))
        return {'status': status, 'headers': {}, 'body': canonical(value), 'url': url}


def publication_fixture(c, full_project, monkeypatch, kind='gitlab'):
    """Inject delivered/current boundary solely to test wire publication, not its gate."""
    p, r, q, root = full_project
    snapshot = c.sn.capture(c.owner, p)
    commit = c.sn.commit_snapshot(snapshot, r, 'Fixture source snapshot')
    ident = 'DELIVERY-WIRE-FIXTURE'
    record = {'git': {r: commit}}
    c.s.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',
                (ident, p, canonical(record).decode(), digest(record), 'delivered', timestamp()))
    def current(delivery):
        assert delivery == ident
        return c.s.one('SELECT * FROM deliveries WHERE id=?', (ident,), True), copy.deepcopy(record)
    monkeypatch.setattr(c.d, 'current', current)
    # Protocol-fixture bypass only: this is explicitly NOT live delivery acceptance.
    monkeypatch.setattr(c.d, 'certify', lambda actor, delivery, check_only=False: {'currently_valid': True})
    target = {'kind': kind, 'owner': 'group/sub' if kind == 'gitlab' else 'group',
              'repository': 'repo', 'base_branch': 'main'}
    wire = WireRepository(kind, target['owner'] + '/repo', commit['commit'])
    c.external.http = wire
    c.external.configure(c.owner, p, r, target, wire.token)
    return ident, r, commit, wire, target


@pytest.mark.parametrize('kind', ['github', 'gitlab'])
def test_publishes_then_reads_back_exact_commit_and_deduplicates(full, full_project, monkeypatch, kind):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch, kind)
    first = c.external.publish(c.owner, delivery, 'Ready', 'Reviewed code')
    assert first['pull_requests'][0]['observed_commit'] == commit['commit']
    assert first['pull_requests'][0]['kind'] == kind
    assert not first['production_deployment_performed']
    second = c.external.publish(c.owner, delivery, 'Ready', 'Reviewed code')
    assert first == second and wire.post_count == 1
    assert len(c.external.status(c.owner, delivery)['observations']) == 1
    assert all(wire.token not in row['body'] for row in c.s.all('SELECT body FROM events'))
    assert any('/' + ('pulls' if kind == 'github' else 'merge_requests') + '/1' == r[1] for r in wire.requests)


@pytest.mark.parametrize('kind', ['github', 'gitlab'])
def test_lost_post_result_is_reconciled_without_duplicate_request(full, full_project, monkeypatch, kind):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch, kind)
    wire.lose_post = True
    with pytest.raises(Fault) as exc:
        c.external.publish(c.owner, delivery, 'Ready', 'Reviewed code')
    assert exc.value.code == 'remote_observation_pending'
    state = c.external.status(c.owner, delivery)
    assert not state['observations'] and state['intents'][0]['status'] == 'pending'
    c.external.publish(c.owner, delivery, 'Ready', 'Reviewed code')
    assert wire.post_count == 1
    assert c.external.status(c.owner, delivery)['intents'][0]['attempts'] == 2


@pytest.mark.parametrize('kind', ['github', 'gitlab'])
@pytest.mark.parametrize('change', ['commit', 'state', 'target', 'project', 'marker', 'source'])
def test_remote_drift_or_incomplete_request_never_records_success(full, full_project, monkeypatch, kind, change):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch, kind)
    def alter(v):
        if change == 'state': v['state'] = 'closed'
        if change == 'marker': v['body' if kind == 'github' else 'description'] = 'Other request'
        if kind == 'github':
            if change == 'commit': v['head']['sha'] = 'f' * 40
            if change == 'target': v['base']['ref'] = 'wrong'
            if change == 'source': v['head']['ref'] = 'wrong'
            if change == 'project': v['head']['repo']['id'] = 99
        else:
            if change == 'commit': v['sha'] = 'f' * 40
            if change == 'target': v['target_branch'] = 'wrong'
            if change == 'source': v['source_branch'] = 'wrong'
            if change == 'project': v['source_project_id'] = 99
    wire.post_hook = alter
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert exc.value.code == 'remote_conflict'
    assert not c.external.status(c.owner, delivery)['observations']


def test_moved_branch_is_not_overwritten(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    wire.remote_branch = 'b' * 40
    with pytest.raises(Fault, match='force push'): c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert wire.post_count == 0


def test_branch_drift_after_request_readback_is_rejected(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    wire.detail_hook = lambda value: setattr(wire, 'remote_branch', 'a' * 40)
    with pytest.raises(Fault, match='Branch changed'): c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert not c.external.status(c.owner, delivery)['observations']


@pytest.mark.parametrize('kind', ['github', 'gitlab'])
def test_missing_branch_uses_real_local_git_push_without_force(full, full_project, monkeypatch, tmp_path, kind):
    import daikibo.remote_repositories as module
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch, kind)
    bare = tmp_path / 'target.git'
    subprocess.run(['git', 'init', '--bare', str(bare)], check=True, capture_output=True)
    wire.remote_branch = None
    def local_push(path, *args, **kwargs):
        assert args[0] == 'push' and '--force' not in args
        assert args[-1] == commit['commit'] + ':refs/heads/daikibo/' + delivery.lower()
        assert wire.token not in repr(args)
        result = git(path, '-c', 'protocol.file.allow=always', 'push', str(bare), args[-1], timeout=30)
        wire.remote_branch = git(bare, 'rev-parse', 'refs/heads/daikibo/' + delivery.lower()).stdout.decode().strip()
        return result
    monkeypatch.setattr(module, 'git', local_push)
    result = c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert result['pull_requests'][0]['commit'] == wire.remote_branch


@pytest.mark.parametrize('field,value', [('base_branch', 'other'), ('repository', 'other')])
def test_reconfiguration_cannot_reuse_old_publish_intent(full, full_project, monkeypatch, field, value):
    c = full
    delivery, repo, commit, wire, target = publication_fixture(c, full_project, monkeypatch)
    c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    target[field] = value
    c.external.configure(c.owner, full_project[0], repo, target)
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert exc.value.code == 'remote_intent_conflict' and wire.post_count == 1


def test_changed_delivery_during_post_is_not_published_as_current(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    original = c.d.current
    def after_post(value):
        def stale(_): raise Fault('stale_delivery', 'Specification changed')
        monkeypatch.setattr(c.d, 'current', stale)
    wire.post_hook = after_post
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert exc.value.code == 'stale_delivery'
    assert not c.external.status(c.owner, delivery)['observations']


def test_real_uncertified_delivery_is_rejected_before_remote_io(full, full_project):
    c = full; p, r, q, _ = full_project
    t = make_task(c, full_project); c.d.configure(c.owner, p, profile(p, r, q, t)); finish_task(c, p, t)
    d = c.d.prepare(c.owner, p)
    c.external.http = lambda *a, **k: pytest.fail('No remote I/O before certified commit')
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, d['id'], 'Ready', 'Not ready')
    assert exc.value.code == 'not_delivered'


@pytest.mark.parametrize('kind', ['github', 'gitlab'])
def test_all_pages_are_read_before_deciding_no_existing_request(kind):
    target = normalized_target({'kind': kind, 'owner': 'group', 'repository': 'repo', 'base_branch': 'main'})
    wire = WireRepository(kind, 'group/repo', 'a' * 40)
    wire.pages = {1: [wire.item('b', 'main', 'marker', i) for i in range(1, 101)],
                  2: [wire.item('b', 'main', 'marker', 101)]}
    api = RepositoryAPI(target, wire.token, wire)
    assert len(api.requests('b')) == 101
    assert len(wire.requests) == 2


def test_duplicate_pagination_and_auth_error_do_not_mean_no_requests():
    target = normalized_target({'kind': 'gitlab', 'owner': 'group', 'repository': 'repo', 'base_branch': 'main'})
    wire = WireRepository('gitlab', 'group/repo', 'a' * 40)
    wire.pages = {1: [wire.item('b', 'main', 'marker', i) for i in range(1, 101)],
                  2: [wire.item('b', 'main', 'marker', 1)]}
    with pytest.raises(Fault): RepositoryAPI(target, wire.token, wire).requests('b')
    def forbidden(*a, **k): return {'status': 403, 'body': b'{}'}
    with pytest.raises(Fault) as exc: RepositoryAPI(target, wire.token, forbidden).requests('b')
    assert exc.value.code == 'remote_error'


@pytest.mark.parametrize('value', ['../main', 'main..x', 'bad.lock', 'trailing/', '-flag', 'double//name', '.hidden', 'main.'])
def test_invalid_base_branch_is_rejected(value):
    with pytest.raises(Fault): normalized_target({'kind': 'gitlab', 'owner': 'group/sub', 'repository': 'repo', 'base_branch': value})


def test_local_status_is_historical_not_remote_assertion(full, full_project, monkeypatch):
    c = full
    delivery, repo, _, wire, _ = publication_fixture(c, full_project, monkeypatch)
    c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    calls = len(wire.requests)
    state = c.invoke(c.owner, 'remote.status', {'delivery': delivery})
    assert state['historical_only'] and len(wire.requests) == calls


def test_publish_runs_via_managed_job_not_only_direct_helper(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    job = c.invoke(c.owner, 'remote.publish', {'delivery': delivery, 'title': 'Ready', 'body': 'Reviewed'})
    assert job['status'] == 'queued' and wire.post_count == 0
    c.jobs.run_one(c.s.one('SELECT * FROM jobs WHERE id=?', (job['id'],)))
    result = c.jobs.get(c.owner, job['id'])
    assert result['status'] == 'succeeded'
    assert result['result']['pull_requests'][0]['commit'] == commit['commit']


def test_conversation_can_submit_certified_publication_job(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    session = 'native-publication-test'
    c.native.attach(c.owner, session, str(full_project[3]), project=full_project[0])
    actions = [{'method': 'remote.publish', 'params': {'delivery': delivery, 'title': 'Ready', 'body': 'Reviewed'}}]
    result = c.native.actions(c.owner, session, actions)
    assert wire.post_count == 0  # Native surface only schedules the managed job.
    jobs = c.s.all("SELECT * FROM jobs WHERE kind='remote.publish'")
    assert len(jobs) == 1, result
    c.jobs.run_one(jobs[0])
    assert c.jobs.get(c.owner, jobs[0]['id'])['status'] == 'succeeded'


def test_multiple_repositories_keep_partial_failure_and_resume_without_duplicate(full, full_project, monkeypatch, tmp_path):
    c = full; p, _, _, _ = full_project
    delivery, first_repo, first_commit, wire1, target1 = publication_fixture(c, full_project, monkeypatch)
    root2 = tmp_path / 'second'; root2.mkdir(); (root2 / 'a.txt').write_text('second repository\n')
    second_repo = c.sn.register(c.owner, p, 'second', str(root2))['id']
    snapshot = c.sn.capture(c.owner, p)
    second_commit = c.sn.commit_snapshot(snapshot, second_repo, 'Second fixture snapshot')
    record = {'git': {first_repo: first_commit, second_repo: second_commit}}
    c.s.execute('UPDATE deliveries SET body=? WHERE id=?', (canonical(record).decode(), delivery))
    monkeypatch.setattr(c.d, 'current', lambda d: (c.s.one('SELECT * FROM deliveries WHERE id=?', (d,), True), copy.deepcopy(record)))
    target2 = {**target1, 'repository': 'second'}
    c.external.configure(c.owner, p, second_repo, target2, wire1.token)
    wire2 = WireRepository('gitlab', target2['owner'] + '/second', second_commit['commit'])
    wire2.lose_post = True
    def routed(url, *a, **k):
        # namespace/path is one encoded API path component.
        return (wire2 if '%2Fsecond' in url else wire1)(url, *a, **k)
    c.external.http = routed
    with pytest.raises(Fault): c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    state = c.external.status(c.owner, delivery)
    assert state['historically_observed_repositories'] == [first_repo]
    assert state['unobserved_repositories'] == [second_repo]
    finished = c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert len(finished['pull_requests']) == 2
    assert wire1.post_count == wire2.post_count == 1
    assert not c.external.status(c.owner, delivery)['unobserved_repositories']


def test_incomplete_listing_refuses_creation_and_does_not_drop_page_101():
    target = normalized_target({'kind': 'gitlab', 'owner': 'group', 'repository': 'repo', 'base_branch': 'main'})
    wire = WireRepository('gitlab', 'group/repo', 'a' * 40)
    wire.pages = {page: [wire.item('b', 'main', 'marker', (page - 1) * 100 + n) for n in range(1, 101)] for page in range(1, 101)}
    with pytest.raises(Fault) as exc: RepositoryAPI(target, wire.token, wire).requests('b')
    assert exc.value.code == 'remote_list_incomplete' and wire.post_count == 0


def test_old_delivered_label_without_current_evidence_cannot_publish(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    def expired(*a, **k): raise Fault('release_gate_denied', 'Required execution evidence disappeared')
    monkeypatch.setattr(c.d, 'certify', expired)
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert exc.value.code == 'release_gate_denied'
    assert not wire.requests and not c.external.status(c.owner, delivery)['observations']


def test_evidence_loss_after_remote_side_effect_keeps_reconciliation_pending(full, full_project, monkeypatch):
    c = full
    delivery, repo, commit, wire, _ = publication_fixture(c, full_project, monkeypatch)
    def after_post(value):
        def expired(*a, **k): raise Fault('release_gate_denied', 'Review evidence no longer available')
        monkeypatch.setattr(c.d, 'certify', expired)
    wire.post_hook = after_post
    with pytest.raises(Fault) as exc: c.external.publish(c.owner, delivery, 'Ready', 'Reviewed')
    assert wire.post_count == 1 and exc.value.code == 'release_gate_denied'
    assert c.external.status(c.owner, delivery)['intents'][0]['status'] == 'pending'


@pytest.mark.parametrize('state,operation', [('verified', 'commit'), ('delivered', 'export')])
def test_formal_git_output_rechecks_actual_delivery_gate(full, full_project, monkeypatch, state, operation):
    c = full; p, r, q, root = full_project
    t = make_task(c, full_project); c.d.configure(c.owner, p, profile(p, r, q, t)); finish_task(c, p, t)
    d = c.d.prepare(c.owner, p)
    # Historical status is not a new live certification. Runtime remains validation.
    body = json.loads(c.s.one('SELECT body FROM deliveries WHERE id=?', (d['id'],))['body'])
    body['git'][r] = {'commit': 'a' * 40, 'git_dir': str(root), 'ref': 'refs/heads/fixture'}
    c.s.execute('UPDATE deliveries SET status=?,body=? WHERE id=?', (state, canonical(body).decode(), d['id']))
    with pytest.raises(Fault) as exc:
        if operation == 'commit': c.d.commit(c.owner, d['id'], 'Cannot use old status')
        else: c.d.export_bundle(c.owner, d['id'], r)
    assert exc.value.code == 'release_gate_denied'
