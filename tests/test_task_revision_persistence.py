"""New revision history survives restarts, migrations and standalone archives."""
from __future__ import annotations
import copy
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.db import SCHEMA_VERSION
from daikibo.knowledge_history import inspect_archive
from test_chunked_knowledge import rewrite
from test_task_definition_revisions import revision_setup, propose, review, apply, changed_body


def test_history_and_pending_proposal_survive_control_restart(revision_setup):
    c,project,t=revision_setup;p=propose(c,t);home=c.s.home
    c.close();restored=Control(home,'validation',start_workers=False)
    try:
        owner=restored.sec.authenticate(None)
        restored.owner=owner
        assert restored.task_revisions.get(owner,p['id'])['status']=='proposed'
        listed=restored.task_revisions.list(owner,project[0])
        assert listed['total']==1 and listed['proposals'][0]['id']==p['id']
        # Registered fixture executable still exists. This is a real fresh run,
        # not a recorded claim copied from before restart.
        result=apply(restored,p)
        assert restored.task_revisions.history(owner,t)['records'][0]['id']==result['history']
    finally:restored.close()


def test_v7_migration_retains_task_and_cannot_backfill_old_history(revision_setup):
    c,project,t=revision_setup;home=c.s.home;old=c.w.task(c.owner,t);c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins');db.execute('DROP TABLE task_revision_history');db.execute('DROP TABLE task_revision_proposals')
    db.execute('PRAGMA user_version=7');db.commit();db.close()
    c=Control(home,'validation',start_workers=False)
    try:
        owner=c.sec.authenticate(None)
        assert c.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert (home/'pre-migration-v7.sqlite3').is_file()
        assert c.w.task(owner,t)['body']==old['body']
        assert c.task_revisions.history(owner,t)['total']==0
        c.w.replan(owner,t,1,'First new historical record')
        assert c.task_revisions.history(owner,t)['total']==1
    finally:c.close()


def export(c,project):
    baseline=c.k.baseline(c.owner,project)
    assert baseline['layout']=='chunked'
    return c.history.export_archive(c.owner,baseline['id'])


def test_archive_contains_old_definition_tests_and_applied_and_pending_proposals(revision_setup):
    c,project,t=revision_setup;p=propose(c,t);apply(c,p)
    pending=propose(c,t,goal='Another possible implementation')
    c.w.replan(c.owner,t,2,'Recheck bindings without rewriting definition')
    a=export(c,project[0]);result=inspect_archive(a['path'],a['sha256'])
    assert result['format']=='daikibo.knowledge-archive.v12'
    assert result['counts']['task_revision_history']==2 and result['counts']['task_revision_proposals']==2
    direct=c.k.export(c.owner,project[0])
    assert len(direct['task_revision_history'])==2 and len(direct['task_revision_proposals'])==2
    assert not result['runtime_restore_supported'] and not result['new_test_or_review_evidence']
    assert c.task_revisions.get(c.owner,pending['id'])['status']=='proposed'


def test_legacy_layout_cannot_silently_drop_new_task_history(revision_setup):
    c,project,t=revision_setup;c.w.replan(c.owner,t,1,'Preserve frozen test plan')
    with pytest.raises(Fault) as exc:c.k.baseline(c.owner,project[0],layout='legacy')
    assert exc.value.code=='legacy_cannot_preserve_task_history'


def test_standalone_revision_archive_needs_no_original_controller(revision_setup,tmp_path):
    c,project,t=revision_setup;apply(c,propose(c,t));a=export(c,project[0])
    target=tmp_path/'portable.dkarchive';shutil.copyfile(a['path'],target)
    home=c.s.home;c.close();shutil.rmtree(home)
    result=inspect_archive(target,a['sha256'])
    assert result['verified'] and result['counts']['task_revision_history']==1


@pytest.mark.parametrize('damage',['missing_history','wrong_before_revision','wrong_plan_digest','rewritten_body',
                                    'fake_result','reset_attempts','candidate_carried_forward','duplicate_revision',
                                    'proposal_history_disconnected'])
def test_history_checksums_do_not_replace_relational_validation(revision_setup,tmp_path,damage):
    c,project,t=revision_setup;apply(c,propose(c,t));a=export(c,project[0])
    def mutate(manifest,rows,objects):
        h=next(r for r in rows if r['section']=='task_revision_history')['row']
        p=next(r for r in rows if r['section']=='task_revision_proposals')['row']
        if damage=='missing_history':rows[:]=[r for r in rows if r['section']!='task_revision_history']
        elif damage=='wrong_before_revision':h['body']['before']['task']['revision']+=1
        elif damage=='wrong_plan_digest':h['body']['before']['test_plan']['digest']='0'*64
        elif damage=='rewritten_body':h['body']['before']['task']['body']['read_artifacts']=[]
        elif damage=='fake_result':p['result']['revision']+=1
        elif damage=='reset_attempts':h['body']['before']['task']['attempts']=4;h['body']['after']['task']['attempts']=0
        elif damage=='candidate_carried_forward':h['body']['after']['task']['candidate']='old-candidate'
        elif damage=='duplicate_revision':
            r=copy.deepcopy(next(r for r in rows if r['section']=='task_revision_history'));r['row']['id']='DUP';rows.append(r)
        elif damage=='proposal_history_disconnected':h['body']['proposal']='unrecorded'
        # Recompute local checksums deliberately to exercise the relational checks.
        h['digest']=digest(h['body']);p['digest']=digest(p['body']);p['binding']=digest({'proposal':p['id'],'body':p['body']})
    sha=rewrite(a['path'],tmp_path/'inconsistent.dkarchive',mutate)
    with pytest.raises(Fault):inspect_archive(tmp_path/'inconsistent.dkarchive',sha)


def test_genuine_dev5_v2_archive_stays_readable_without_v3_records():
    fixture=Path(__file__).with_name('fixtures')/'dev5-v2-history.dkarchive'
    expected=fixture.with_suffix('.sha256').read_text().strip()
    value=inspect_archive(fixture,expected)
    assert value['verified'] and value['format']=='daikibo.knowledge-archive.v2'
    assert value['counts']['task_revision_history']==0
    assert value['counts']['artifacts']==1 and not value['new_test_or_review_evidence']


def test_proposal_listing_can_resume_without_remembering_an_id(revision_setup):
    c,project,t=revision_setup;first=propose(c,t);second=propose(c,t,goal='Alternative')
    page=c.task_revisions.list(c.owner,project[0],limit=1)
    next_page=c.task_revisions.list(c.owner,project[0],offset=1,limit=1,expected_snapshot=page['snapshot'])
    assert {r['id'] for r in page['proposals']+next_page['proposals']}=={first['id'],second['id']}
    c.task_revisions.withdraw(c.owner,first['id'],first['digest'],'Keep alternative')
    with pytest.raises(Fault):c.task_revisions.list(c.owner,project[0],offset=1,limit=1,expected_snapshot=page['snapshot'])


def test_complete_backup_preserves_revision_receipts_and_pending_proposals(revision_setup,tmp_path):
    from daikibo.operations import restore_backup
    c,project,t=revision_setup;done=apply(c,propose(c,t));pending=propose(c,t,goal='A new alternative')
    b=c.ops.backup(c.owner);home=tmp_path/'restore'
    restore_backup(b['path'],home,b['sha256'])
    restored=Control(home,'validation',start_workers=False)
    try:
        actor=restored.sec.authenticate(None)
        assert restored.g.receipt(done['review_receipt'])['role']=='impact'
        assert restored.task_revisions.get(actor,pending['id'])['status']=='proposed'
        assert restored.task_revisions.history(actor,t)['total']==1
        assert restored.task_revisions.history_record(actor,done['history'])['body']['after']['task']['revision']==2
    finally:restored.close()
