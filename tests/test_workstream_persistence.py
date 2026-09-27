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
from test_reviewed_breakdowns import setup, adopt
from test_delegated_workstreams import activate_scope, ws_propose, review_scope, withdraw, finish, second_unit
from test_chunked_knowledge import rewrite


def export(c,p):
    b=c.k.baseline(c.owner,p)
    assert b['layout']=='chunked'
    return c.history.export_archive(c.owner,b['id'])


def test_restart_preserves_proposal_reviews_and_hierarchy(setup):
    c=setup[0];adopt(setup);parent=activate_scope(setup);p=ws_propose(setup,parent=parent);home=c.s.home
    review_scope(c,p['scope']);c.close()
    new=Control(home,'validation',start_workers=False)
    try:
        new.owner=new.sec.authenticate(None)
        assert new.workstreams.activate(new.owner,p['scope'])['status']=='active'
        assert new.workstreams.get(new.owner,p['scope'])['parent']==parent
        assert new.workstreams.status(new.owner,parent)['current']
    finally:new.close()


def test_v8_migration_is_additive_and_preserves_tasks(setup):
    c=setup[0];p=setup[1];t=setup[6];home=c.s.home;original=c.w.task(c.owner,t);c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins')
    for table in ('workstream_records','workstream_packets','workstreams'):db.execute('DROP TABLE '+table)
    db.execute('PRAGMA user_version=8');db.commit();db.close()
    new=Control(home,'validation',start_workers=False)
    try:
        owner=new.sec.authenticate(None)
        assert new.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert (home/'pre-migration-v8.sqlite3').exists()
        assert new.w.task(owner,t)==original
        assert new.s.one('SELECT count(*) n FROM workstreams')['n']==0
    finally:new.close()


def test_complete_scope_history_self_contained_and_direct_export(setup,tmp_path):
    c,p,r,q,program,d,t,units=setup;adopt(setup)
    original=activate_scope(setup);finish(c,p,t);c.workstreams.finish(c.owner,original)
    replacement=activate_scope(setup,previous=original);withdraw(c,replacement)
    pending=ws_propose(setup)['scope']
    archive=export(c,p);result=inspect_archive(archive['path'],archive['sha256'])
    # The current baseline contains immutable Assurance material, so the
    # exporter must use the additive v12 archive.  Legacy v8 readability is
    # exercised independently by test_legacy_execution_telemetry_archive.
    assert result['format']=='daikibo.knowledge-archive.v12'
    assert result['counts']['workstreams']==3 and result['counts']['workstream_records']==4
    assert not result['runtime_restore_supported'] and not result['new_test_or_review_evidence']
    direct=c.k.export(c.owner,p)
    assert len(direct['workstream_history']['workstreams'])==3
    target=tmp_path/'separate.dkarchive';shutil.copyfile(archive['path'],target)
    home=c.s.home;c.close();shutil.rmtree(home)
    assert inspect_archive(target,archive['sha256'])['verified']


def test_legacy_cannot_omit_scope_history(setup):
    c=setup[0];adopt(setup);activate_scope(setup)
    with pytest.raises(Fault) as e:c.k.baseline(c.owner,setup[1],layout='legacy')
    assert e.value.code=='legacy_cannot_preserve_workstreams'


@pytest.mark.parametrize('damage',['missing_scope','missing_packet','missing_adoption','packet_scope','packet_order','root_program',
                                   'drop_obligation','wrong_task','wrong_parent','scope_digest','packet_digest','event_digest','false_release'])
def test_history_relations_reject_corruption_even_with_new_container_checksums(setup,tmp_path,damage):
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup);finish(c,p,t);c.workstreams.finish(c.owner,w);a=export(c,p)
    def mutate(manifest,rows,objects):
        row=next(r for r in rows if r['section']=='workstreams')['row']
        packet=next(r for r in rows if r['section']=='workstream_packets')['row']
        event=next(r for r in rows if r['section']=='workstream_records' and r['row']['kind']=='finish')['row']
        if damage=='missing_scope':rows[:]=[r for r in rows if r['section']!='workstreams']
        elif damage=='missing_packet':rows[:]=[r for r in rows if r['section']!='workstream_packets']
        elif damage=='missing_adoption':rows[:]=[r for r in rows if not(r['section']=='workstream_records' and r['row']['kind']=='adopt')]
        elif damage=='packet_scope':packet['scope']='ABSENT'
        elif damage=='packet_order':packet['ordinal']=99
        elif damage=='root_program':row['program']='OTHER'
        elif damage=='drop_obligation':row['body']['selection']['obligations']=[];row['digest']=digest(row['body'])
        elif damage=='wrong_task':row['body']['selection']['tasks']=[];row['digest']=digest(row['body'])
        elif damage=='wrong_parent':row['parent']=row['id']
        elif damage=='scope_digest':row['digest']='0'*64
        elif damage=='packet_digest':packet['digest']='0'*64
        elif damage=='event_digest':event['digest']='0'*64
        elif damage=='false_release':event['body']['report']['deploy_ready']=True;event['digest']=digest(event['body'])
    target=tmp_path/'modified.dkarchive';h=rewrite(a['path'],target,mutate)
    with pytest.raises(Fault):inspect_archive(target,h)


def test_history_tables_refuse_normal_rewrite(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    for sql,args in [('UPDATE workstreams SET body=? WHERE id=?',('{}',w)),
                     ('UPDATE workstream_packets SET body=? WHERE scope=?',('{}',w)),
                     ('UPDATE workstream_records SET body=? WHERE scope=?',('{}',w)),
                     ('DELETE FROM workstream_records WHERE scope=?',(w,))]:
        with pytest.raises(sqlite3.IntegrityError):c.s.execute(sql,args)


def test_full_backup_retains_operational_scope_evidence(setup,tmp_path):
    from daikibo.operations import restore_backup
    c,p,r,q,program,d,t,units=setup;adopt(setup);w=activate_scope(setup);finish(c,p,t);c.workstreams.finish(c.owner,w)
    backup=c.ops.backup(c.owner);home=tmp_path/'restored'
    # Use the standard complete backup path, not the historical specification ZIP.
    result=restore_backup(backup['path'],home,backup['sha256'])
    new=Control(home,'validation',start_workers=False)
    try:
        owner=new.sec.authenticate(None)
        assert new.workstreams.status(owner,w)['current']
        assert new.workstreams.completion(owner,w)['ready']
        assert new.workstreams.finish(owner,w)['replayed']
    finally:new.close()


def test_missing_adoption_record_not_silently_treated_as_adopted(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    c.s.execute('DROP TRIGGER workstream_records_no_delete')
    c.s.execute("DELETE FROM workstream_records WHERE scope=? AND kind='adopt'",(w,))
    assert not c.workstreams.status(c.owner,w)['current']
