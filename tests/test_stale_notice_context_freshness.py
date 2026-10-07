import pytest

from daikibo.common import Fault, digest, parse_json


def test_native_ack_rejects_source_from_previous_notice_version(full, full_project):
    c = full
    project, _, _, root = full_project
    c.native.attach(c.owner, 'notice-version', str(root), project=project)
    c.g.inbox(project, 'warning', 'same-ref', {'message': 'Original notice'}, 'warning')
    original = c.s.one("SELECT * FROM inbox WHERE project=? AND ref='same-ref'", (project,))
    first_publication = c.s.one("SELECT seq FROM events WHERE kind='notification_published' "
                                 "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",
                                 (original['id'],))
    old_input = c.native.input(c.owner, 'notice-version', 'I will review the notice.', turn_id='old')

    c.g.inbox(project, 'warning', 'same-ref', {'message': 'Updated notice'}, 'warning')
    current = c.s.one("SELECT * FROM inbox WHERE project=? AND ref='same-ref'", (project,))
    assert current['id'] == original['id']
    current_publication = c.s.one("SELECT seq FROM events WHERE kind='notification_published' "
                                  "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",
                                  (current['id'],))
    assert current_publication['seq'] > first_publication['seq']
    current_digest = digest(current['body'].encode())

    with pytest.raises(Fault) as error:
        c.native.acknowledge(c.owner, 'notice-version', current['id'], old_input['source'],
                             'I will review the notice.', expected_digest=current_digest)
    assert error.value.code == 'stale_user_input'
    assert c.s.one('SELECT status FROM inbox WHERE id=?', (current['id'],))['status'] == 'open'

    new_input = c.native.input(c.owner, 'notice-version', 'I reviewed the updated notice.', turn_id='new')
    result = c.native.acknowledge(c.owner, 'notice-version', current['id'], new_input['source'],
                                  'I reviewed the updated notice.', expected_digest=current_digest)
    assert result['acknowledged']
    assert c.s.one('SELECT status FROM inbox WHERE id=?', (current['id'],))['status'] == 'acknowledged'
    recorded = parse_json(c.s.one("SELECT body FROM events WHERE kind='inbox_acknowledged' "
                                   "ORDER BY seq DESC LIMIT 1", ())['body'])
    assert recorded['notice_version_digest']
    assert recorded['source_registered_seq'] > recorded['notification_published_seq']


@pytest.mark.parametrize('times', [(100.0, 100.0, 100.0), (100.0, 200.0, 150.0)])
def test_native_ack_uses_publication_order_when_clocks_tie_or_move_back(
        full, full_project, monkeypatch, times):
    c = full
    project, _, _, root = full_project
    c.native.attach(c.owner, 'notice-clock', str(root), project=project)
    clock = {'now': times[0]}
    monkeypatch.setattr('daikibo.governance.timestamp', lambda: clock['now'])
    monkeypatch.setattr('daikibo.knowledge.timestamp', lambda: clock['now'])
    monkeypatch.setattr('daikibo.native.timestamp', lambda: clock['now'])
    c.sec.clock = lambda: clock['now']

    c.g.inbox(project, 'warning', 'clock-notice', {'message': 'First version'}, 'warning')
    clock['now'] = times[1]
    prior = c.native.input(c.owner, 'notice-clock', 'I saw the first version.', turn_id='first')
    clock['now'] = times[2]
    c.g.inbox(project, 'warning', 'clock-notice', {'message': 'Second version'}, 'warning')
    notice = c.s.one("SELECT * FROM inbox WHERE project=? AND ref='clock-notice'", (project,))
    new_digest = digest(notice['body'].encode())

    with pytest.raises(Fault) as error:
        c.native.acknowledge(c.owner, 'notice-clock', notice['id'], prior['source'],
                             'I saw the first version.', expected_digest=new_digest)
    assert error.value.code == 'stale_user_input'

    clock['now'] = times[2]
    current = c.native.input(c.owner, 'notice-clock', 'I saw the second version.', turn_id='second')
    result = c.native.acknowledge(c.owner, 'notice-clock', notice['id'], current['source'],
                                  'I saw the second version.', expected_digest=new_digest)
    assert result['acknowledged']


def test_identical_notice_resend_keeps_version_time_and_reopens(full, full_project):
    c = full
    project = full_project[0]
    body = {'message': 'Same notice'}
    c.g.inbox(project, 'warning', 'same-body', body, 'warning')
    notice = c.s.one("SELECT * FROM inbox WHERE project=? AND ref='same-body'", (project,))
    publication = c.s.one("SELECT seq FROM events WHERE kind='notification_published' "
                          "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",
                          (notice['id'],))
    c.s.execute("UPDATE inbox SET status='acknowledged' WHERE id=?", (notice['id'],))

    c.g.inbox(project, 'warning', 'same-body', body, 'warning')
    resent = c.s.one('SELECT * FROM inbox WHERE id=?', (notice['id'],))
    repeated_publication = c.s.one("SELECT seq FROM events WHERE kind='notification_published' "
                                   "AND json_extract(body,'$.item')=? ORDER BY seq DESC LIMIT 1",
                                   (notice['id'],))
    assert resent['created'] == notice['created']
    assert resent['status'] == 'open'
    assert repeated_publication['seq'] == publication['seq']


def test_context_fresh_checks_live_artifact_status(full, full_project):
    c = full
    project, repo, requirement, _ = full_project
    draft = c.k.propose(c.owner, project, 'finding', {
        'title': 'Unadopted finding', 'statement': 'A relevant finding is still a draft.'})
    task = c.w.create(c.owner, project, {
        'title': 'Use the finding', 'goal': 'Read the project materials',
        'read_artifacts': [requirement, draft['id']], 'write_paths': ['calc.py'],
        'acceptance': ['AC-ADD'], 'dependencies': [], 'repos': [repo], 'non_goals': [],
    })['id']

    before = c.ctx.task_context(c.owner, task)
    assert c.ctx.fresh(c.owner, before['id']) == {'id': before['id'], 'fresh': True, 'stale': []}

    c.k.accept(c.owner, draft['id'], draft['revision'])
    stale = c.ctx.fresh(c.owner, before['id'])
    assert not stale['fresh']
    assert 'artifact_status_changed:' + draft['id'] in stale['stale']

    after = c.ctx.task_context(c.owner, task)
    assert next(item for item in after['package']['mandatory']['artifacts']
                if item['id'] == draft['id'])['status'] == 'accepted'
    assert c.ctx.fresh(c.owner, after['id'])['fresh']
