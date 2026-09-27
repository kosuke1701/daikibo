"""Return proposal recovery and portable complete review-tree integrity."""
import copy
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path
import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.db import SCHEMA_VERSION
from daikibo.knowledge_history import inspect_archive
from daikibo import archive_chunks as chunks
from test_reviewed_breakdowns import setup, adopt
from test_delegated_workstreams import activate_scope
from test_scope_returns import begin, ready, apply_ready, review_level


def rewrite(source,target,mutate):
    """Recompute transport and section hashes so *relational* validation is tested."""
    with zipfile.ZipFile(source) as z:
        payload=parse_json(z.read('snapshot.json'));header=parse_json(z.read('manifest.json'))
        objects={h:z.read('objects/'+h) for h in payload['objects']}
    old={r['sha256'] for r in payload['records']['chunks']}
    rows=[parse_json(line,limit=chunks.MAX_RECORD_BYTES) for line in b''.join(objects[r['sha256']] for r in payload['records']['chunks']).splitlines()]
    mutate(payload,rows,objects)
    data=b''.join(canonical(row)+b'\n' for row in rows)
    for h in old:objects.pop(h,None)
    parts=[]
    for offset in range(0,len(data),1024):
        block=data[offset:offset+1024];h=digest(block);objects[h]=block;parts.append({'sha256':h,'bytes':len(block)})
    payload['records']={'chunks':parts,'bytes':len(data),'sha256':digest(data),
        'counts':{s:sum(r['section']==s for r in rows) for s in chunks.sections_for(payload)}}
    payload['objects']={h:{'bytes':len(b)} for h,b in objects.items()}
    raw=canonical(payload);header['snapshot']={'sha256':digest(raw),'bytes':len(raw)}
    with zipfile.ZipFile(target,'w') as z:
        z.writestr('manifest.json',canonical(header));z.writestr('snapshot.json',raw)
        for h,b in objects.items():z.writestr('objects/'+h,b)
    return chunks.file_digest(target)


def export(c,p):
    b=c.k.baseline(c.owner,p);assert b['layout']=='chunked'
    result=c.history.export_archive(c.owner,b['id'])
    assert inspect_archive(result['path'],result['sha256'])['verified']
    return result


def test_restart_mid_synthesis_keeps_observed_reviews_and_next_level(setup):
    c=setup[0];w,p=begin(setup);review_level(c,p['id'])
    before=c.scope_returns.advance(c.owner,p['id']);home=c.s.home;c.close()
    other=Control(home,'validation',start_workers=False)
    try:
        other.owner=other.sec.authenticate(None)
        assert other.scope_returns.advance(other.owner,p['id'])==before
        assert apply_ready(other,p)['status']=='withdrawn'
    finally:other.close()


def test_schema9_migration_retains_preexisting_scope_and_creates_empty_return_tables(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup);before=c.workstreams.get(c.owner,w);home=c.s.home;c.close()
    db=sqlite3.connect(home/'state.sqlite3')
    db.execute('DROP TABLE program_origins');db.execute('DROP TABLE scope_return_packets');db.execute('DROP TABLE scope_returns');db.execute('PRAGMA user_version=9');db.commit();db.close()
    other=Control(home,'validation',start_workers=False)
    try:
        owner=other.sec.authenticate(None)
        assert other.s.one('PRAGMA user_version')['user_version']==SCHEMA_VERSION
        assert (home/'pre-migration-v9.sqlite3').exists()
        assert other.workstreams.get(owner,w)==before
        assert other.scope_returns.list(owner,w)['items']==[]
        assert other.workstreams.status(owner,w)['current']
    finally:other.close()


def test_current_v12_archive_remains_readable_after_scope_history_addition(setup):
    c=setup[0];adopt(setup);w=activate_scope(setup)
    old=export(c,setup[1]);assert old['format']=='daikibo.knowledge-archive.v12'
    c.scope_returns.propose(c.owner,w,'Return with preserved history')
    new=export(c,setup[1]);assert new['format']=='daikibo.knowledge-archive.v12'
    assert inspect_archive(old['path'],old['sha256'])['verified']


def test_new_history_standalone_after_original_state_removed(setup,tmp_path):
    c=setup[0];w,p=begin(setup);apply_ready(c,p)
    archive=export(c,setup[1]);direct=c.k.export(c.owner,setup[1])
    assert len(direct['scope_return_history']['scope_returns'])==1
    result=inspect_archive(archive['path'],archive['sha256'])
    assert result['format']=='daikibo.knowledge-archive.v12' and result['counts']['scope_returns']==1
    assert not result['runtime_restore_supported'] and not result['new_test_or_review_evidence']
    target=tmp_path/'separate.zip';shutil.copyfile(archive['path'],target);home=c.s.home;c.close();shutil.rmtree(home)
    assert inspect_archive(target,archive['sha256'])['verified']


def test_archive_records_open_abandoned_applied_proposals_and_all_old_syntheses(setup):
    c=setup[0];w,p=begin(setup,budget=100000);ready(c,p['id'])
    c.rt.review(c.owner,p['leaves'][0]['id'],'impact','markers');ready(c,p['id'])
    other=c.scope_returns.propose(c.owner,w,'Consider a different reason');c.scope_returns.abandon(c.owner,other['id'],'Keep original')
    c.scope_returns.propose(c.owner,w,'Another pending alternative')
    apply_ready(c,p);a=export(c,setup[1]);counts=inspect_archive(a['path'],a['sha256'])['counts']
    assert counts['scope_returns']==3
    assert counts['scope_return_packets']==c.s.one('SELECT count(*) n FROM scope_return_packets')['n']


def test_operational_backup_restores_pending_synthesis_and_runtime_evidence(setup,tmp_path):
    from daikibo.operations import restore_backup
    c=setup[0];w,p=begin(setup);review_level(c,p['id']);before=c.scope_returns.advance(c.owner,p['id'])
    backup=c.ops.backup(c.owner);home=tmp_path/'restored'
    restore_backup(backup['path'],home,backup['sha256'])
    other=Control(home,'validation',start_workers=False)
    try:
        other.owner=other.sec.authenticate(None)
        assert other.scope_returns.advance(other.owner,p['id'])==before
        assert apply_ready(other,p)['status']=='withdrawn'
    finally:other.close()


@pytest.mark.parametrize('damage',['missing_proposal','missing_leaf','missing_synthesis','source_order','wrong_project','proposal_digest',
    'current_artifact_body','drop_original_obligation','wrong_parent','wrong_child_digest','failed_child','uncovered_child',
    'duplicate_child','skipped_level','missing_event','event_omits_tasks','false_deploy_ready','result_points_elsewhere','root_is_leaf'])
def test_rechecks_relations_with_valid_transport_checksums(setup,tmp_path,damage):
    c=setup[0];w,p=begin(setup,budget=100000);apply_ready(c,p);archive=export(c,setup[1])
    # The helper's no-op rewrite is validated first; failures below cannot be due
    # merely to an incompatible list of section names in the helper itself.
    unchanged=tmp_path/'unchanged.zip';same=rewrite(archive['path'],unchanged,lambda *a:None)
    assert inspect_archive(unchanged,same)['verified']
    def mutate(manifest,rows,objects):
        proposal=next(r['row'] for r in rows if r['section']=='scope_returns')
        leaf=next(r['row'] for r in rows if r['section']=='scope_return_packets' and r['row']['level']==0)
        synth=next(r['row'] for r in rows if r['section']=='scope_return_packets' and r['row']['level']>0)
        event=next(r['row'] for r in rows if r['section']=='workstream_records' and r['row']['kind']=='withdraw')
        if damage=='missing_proposal':rows[:]=[r for r in rows if r['section']!='scope_returns']
        elif damage=='missing_leaf':rows[:]=[r for r in rows if not(r['section']=='scope_return_packets' and r['row']['level']==0)]
        elif damage=='missing_synthesis':rows[:]=[r for r in rows if not(r['section']=='scope_return_packets' and r['row']['level']>0)]
        elif damage=='source_order':leaf['ordinal']=8
        elif damage=='wrong_project':proposal['project']='another'
        elif damage=='proposal_digest':proposal['digest']='0'*64
        elif damage=='current_artifact_body':proposal['body']['material']['artifacts'][0]['body']['statement']='changed'
        elif damage=='drop_original_obligation':proposal['body']['material']['retained_selection']['obligations']=[]
        elif damage=='wrong_parent':proposal['body']['material']['parent']={'id':'absent','status':'active','digest':'0'*64}
        elif damage=='wrong_child_digest':synth['body']['children'][0]['packet_digest']='0'*64
        elif damage=='failed_child':synth['body']['children'][0]['result']['verdict']='fail'
        elif damage=='uncovered_child':synth['body']['children'][0]['result']['covered']=[]
        elif damage=='duplicate_child':synth['body']['children']*=2
        elif damage=='skipped_level':synth['level']+=1;synth['body']['level']+=1
        elif damage=='missing_event':rows[:]=[r for r in rows if not(r['section']=='workstream_records' and r['row']['kind']=='withdraw')]
        elif damage=='event_omits_tasks':event['body']['tasks_cancelled']=['Task-removed']
        elif damage=='false_deploy_ready':event['body']['deploy_ready']=True
        elif damage=='result_points_elsewhere':proposal['result']['record']='absent'
        elif damage=='root_is_leaf':proposal['result']['root_packet']=leaf['id']
    changed=tmp_path/'changed.zip';h=rewrite(archive['path'],changed,mutate)
    with pytest.raises(Fault):inspect_archive(changed,h)


def test_new_history_immutable_under_normal_operations(setup):
    c=setup[0];w,p=begin(setup)
    for table in ('scope_returns','scope_return_packets'):
        with pytest.raises(sqlite3.IntegrityError):c.s.execute('UPDATE '+table+' SET body=?',('{}',))
        with pytest.raises(sqlite3.IntegrityError):c.s.execute('DELETE FROM '+table)
