import copy
import shutil
import sqlite3
from pathlib import Path
import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.db import SCHEMA_VERSION
from daikibo.knowledge_history import inspect_archive
from test_reviewed_breakdowns import setup
from test_subplan_drafts import draft,prepare_draft_admission,review_draft
from test_scope_return_persistence import rewrite


def export(c,project):
    b=c.k.baseline(c.owner,project);assert b['layout']=='chunked'
    result=c.history.export_archive(c.owner,b['id']);assert inspect_archive(result['path'],result['sha256'])['verified']
    return result


def composed(setup):
    c=setup[0];leaf=draft(setup);root=draft(setup,units=[],children=[leaf['id']])
    prepare_draft_admission(
        c,root['id'],requirements=[setup[3]],task_ids=[setup[6]],recursive=True,
    )
    review_draft(c,root['id'],True)
    result=c.subplans.compose(c.owner,root['id'])
    return leaf,root,result


def test_restart_retains_pending_packets_and_composition_replay(setup):
    c=setup[0];a,b,result=composed(setup);home=c.s.home;c.close()
    other=Control(home,'validation',start_workers=False)
    try:
        other.owner=other.sec.authenticate(None)
        assert other.subplans.audit(other.owner,b['id'])['current']
        replay=other.subplans.compose(other.owner,b['id']);assert replay['replayed'] and replay['breakdown']==result['breakdown']
    finally:other.close()


def test_migrate_actual_schema10_shape_without_fabricated_history(setup):
    c=setup[0];home=c.s.home;q=setup[3];before=c.k.artifact(c.owner,q);c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins')
    for table in ('subplan_compositions','subplan_packets','subplans'):db.execute('DROP TABLE '+table)
    db.execute('PRAGMA user_version=10');db.commit();db.close()
    other=Control(home,'validation',start_workers=False)
    try:
        owner=other.sec.authenticate(None)
        assert other.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION==16
        assert (home/'pre-migration-v10.sqlite3').is_file()
        assert other.k.artifact(owner,q)==before
        assert other.subplans.list(owner,setup[4])['total']==0
        for table in ('local_execution_proposals','local_execution_packets','local_execution_records'):
            assert other.s.one('SELECT count(*) AS n FROM '+table)['n']==0
    finally:other.close()


def test_full_backup_restores_drafts_reviews_and_result(setup,tmp_path):
    from daikibo.operations import restore_backup
    c=setup[0];a,b,result=composed(setup);backup=c.ops.backup(c.owner)
    home=tmp_path/'recovered';restore_backup(backup['path'],home,backup['sha256'])
    other=Control(home,'validation',start_workers=False)
    try:
        owner=other.sec.authenticate(None)
        assert other.subplans.audit(owner,b['id'])['current']
        assert other.subplans.compose(owner,b['id'])['breakdown']==result['breakdown']
    finally:other.close()


def test_archive_without_source_database_and_direct_export(setup,tmp_path):
    c=setup[0];a,b,result=composed(setup);out=export(c,setup[1])
    assert out['format']=='daikibo.knowledge-archive.v12'
    report=inspect_archive(out['path'],out['sha256']);assert report['counts']['subplans']==2 and report['counts']['subplan_compositions']==1
    assert report['new_test_or_review_evidence'] is False
    spec=c.k.export(c.owner,setup[1]);assert len(spec['subplan_history']['subplans'])==2
    target=tmp_path/'alone.zip';shutil.copyfile(out['path'],target);home=c.s.home;c.close();shutil.rmtree(home)
    assert inspect_archive(target,out['sha256'])['verified']


def test_unaccepted_and_stale_partial_history_is_still_retained(setup):
    c,p,r,q,program,d,t,units=setup
    extra=c.k.propose(c.owner,p,'design',{'title':'Draft','statement':'First'})
    v=draft(setup,context=[extra['id']]);c.k.revise(c.owner,extra['id'],1,{'title':'Draft','statement':'New idea'},'Exploration')
    assert not c.subplans.audit(c.owner,v['id'])['current']
    out=export(c,p);assert inspect_archive(out['path'],out['sha256'])['verified']
    with pytest.raises(Fault) as exc:c.k.baseline(c.owner,p,layout='legacy')
    assert exc.value.code=='legacy_cannot_preserve_subplans'


@pytest.mark.parametrize('damage',['missing_composition','plan','packet','composition_digest','packet_order','marker','child_digest','missing_child','missing_packet','missing_review_ref','review_duplicate','true_release','root_units','artifact_material'])
def test_archive_rejects_missing_or_inconsistent_plan_history(setup,tmp_path,damage):
    c=setup[0];a,b,result=composed(setup);out=export(c,setup[1])
    # Rehashed transportation must be valid first, so the failures below test relations.
    same=tmp_path/'same.zip';h=rewrite(out['path'],same,lambda *a:None);assert inspect_archive(same,h)['verified']
    def mutate(manifest,rows,objects):
        root=next(r['row'] for r in rows if r['section']=='subplans' and r['row']['id']==b['id'])
        pkt=next(r['row'] for r in rows if r['section']=='subplan_packets' and r['row']['subplan']==b['id'])
        composition=next(r['row'] for r in rows if r['section']=='subplan_compositions')
        if damage=='missing_composition':rows[:]=[r for r in rows if r['section']!='subplan_compositions']
        elif damage=='plan':root['digest']='0'*64
        elif damage=='packet':pkt['digest']='0'*64
        elif damage=='composition_digest':composition['digest']='0'*64
        elif damage=='packet_order':pkt['ordinal']=999
        elif damage=='marker':pkt['body']['required_coverage']=[]
        elif damage=='child_digest':root['body']['children'][0]['digest']='0'*64
        elif damage=='missing_child':rows[:]=[r for r in rows if not(r['section']=='subplans' and r['row']['id']==a['id'])]
        elif damage=='missing_packet':rows[:]=[r for r in rows if not(r['section']=='subplan_packets' and r['row']['id']==pkt['id'])]
        elif damage=='missing_review_ref':composition['body']['reviews'].pop();composition['digest']=digest(composition['body'])
        elif damage=='review_duplicate':composition['body']['reviews'][0]['receipt']=composition['body']['reviews'][1]['receipt'];composition['digest']=digest(composition['body'])
        elif damage=='true_release':composition['body']['deploy_ready']=True;composition['digest']=digest(composition['body'])
        elif damage=='root_units':composition['body']['units_digest']='0'*64;composition['digest']=digest(composition['body'])
        elif damage=='artifact_material':pkt['body']['serialized_fragment']=pkt['body']['serialized_fragment'].replace('Arithmetic','Fabricated')
    target=tmp_path/'changed.zip';h=rewrite(out['path'],target,mutate)
    with pytest.raises(Fault):inspect_archive(target,h)


def test_history_tables_are_append_only(setup):
    c=setup[0];composed(setup)
    for table in ('subplans','subplan_packets','subplan_compositions'):
        with pytest.raises(sqlite3.IntegrityError):c.s.execute('UPDATE '+table+' SET body=?',('{}',))
        with pytest.raises(sqlite3.IntegrityError):c.s.execute('DELETE FROM '+table)
