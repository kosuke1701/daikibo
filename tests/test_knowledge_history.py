"""Immutable specification views and portable verification; not runtime certification."""
import base64
import copy
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from daikibo.common import Actor, Fault, canonical, digest, parse_json
from daikibo.control import Control
from daikibo.gitops import git
from daikibo.knowledge_history import inspect_archive
from daikibo.operations import restore_backup


def add_revisions(c,project):
    draft=c.k.propose(c.owner,project,'design',{'title':'Design','statement':'Original design'})
    c.k.revise(c.owner,draft['id'],1,{'title':'Design','statement':'Second design'},'Feasibility finding')
    c.k.revise(c.owner,draft['id'],2,{'title':'Design','statement':'Third design'},'Resolved interface conflict')
    return draft['id']


def rewrite_archive(source,target,mutate):
    with zipfile.ZipFile(source) as archive:
        manifest=parse_json(archive.read('manifest.json'))
        snapshot=parse_json(archive.read('snapshot.json'))
    mutate(snapshot)
    data=canonical(snapshot);manifest['files']['snapshot.json']={'bytes':len(data),'sha256':digest(data)}
    with zipfile.ZipFile(target,'w') as archive:
        archive.writestr('snapshot.json',data);archive.writestr('manifest.json',canonical(manifest))
    return digest(target.read_bytes())


def test_baseline_keeps_original_text_all_revisions_and_classifications(full,full_project):
    c=full;p=full_project[0];design=add_revisions(c,p)
    base=c.k.baseline(c.owner,p);saved=c.history.get(c.owner,base['id'])
    payload=parse_json(c.s.blob_get(saved['snapshot_blob']));spec=payload['specifications']
    assert spec['source_contents']
    for src in spec['sources']:
        assert digest(spec['source_contents'][src['id']].encode())==src['blob']
    history=[r for r in spec['revisions'] if r['artifact']==design]
    assert [r['revision'] for r in history]==[1,2,3]
    assert [r['reason'] for r in history]==['initial proposal','Feasibility finding','Resolved interface conflict']
    assert spec['dispositions'][0]['refs']==[full_project[2]]
    assert c.history.verify(c.owner,base['id'])['git_view_verified']
    assert saved['runtime_restore_supported'] is False


def test_artifact_history_paginates_without_dropping_versions(full,full_project):
    c=full;ident=add_revisions(c,full_project[0])
    first=c.history.artifact_history(c.owner,ident,limit=2)
    second=c.history.artifact_history(c.owner,ident,limit=2,offset=first['next_offset'])
    assert [r['revision'] for r in first['revisions']+second['revisions']]==[1,2,3]
    assert second['next_offset'] is None
    assert c.invoke(c.owner,'artifact.history',{'artifact':ident})['current_revision']==3


def test_portable_archive_includes_uninterpreted_document_bytes(full,full_project,tmp_path):
    c=full;p=full_project[0];raw=b'unknown-format\x00\xff original bytes'
    document=c.documents.register(c.owner,p,base64.b64encode(raw).decode(),'attachment.bin')
    baseline=c.k.baseline(c.owner,p);archive=c.history.export_archive(c.owner,baseline['id'])
    result=inspect_archive(archive['path'],archive['sha256'])
    assert result['verified'] and result['counts']['documents']==1
    with zipfile.ZipFile(archive['path']) as z:
        contents=parse_json(z.read('snapshot.json'))['specifications']['document_contents']
    assert base64.b64decode(contents[document['id']])==raw
    assert not result['new_test_or_review_evidence']


def test_old_baseline_export_does_not_follow_new_current_specification(full,full_project):
    c=full;p=full_project[0];design=add_revisions(c,p)
    old=c.k.baseline(c.owner,p);archive=c.history.export_archive(c.owner,old['id'])
    c.k.revise(c.owner,design,3,{'title':'Design','statement':'Fourth design'},'Later change')
    new=c.k.baseline(c.owner,p)
    again=c.history.export_archive(c.owner,old['id'])
    assert again['sha256']==archive['sha256']
    assert c.history.get(c.owner,new['id'])['counts']['revisions']==5  # 4 design plus one requirement
    assert c.history.get(c.owner,old['id'])['counts']['revisions']==4
    assert c.history.verify(c.owner,old['id'])['git_view_verified']
    first=c.history.list(c.owner,p,limit=1)
    assert first['next_offset']==1
    assert c.history.list(c.owner,p,offset=1)['baselines'][0]['id']==new['id']


def test_portable_verification_needs_no_original_db_or_git(full,full_project,tmp_path):
    c=full;base=c.k.baseline(c.owner,full_project[0]);export=c.history.export_archive(c.owner,base['id'])
    portable=tmp_path/'portable.zip';shutil.copyfile(export['path'],portable)
    home=c.s.home;c.close();shutil.rmtree(home)
    result=inspect_archive(portable,export['sha256'])
    assert result['verified'] and result['counts']['sources']==1
    assert result['runtime_restore_supported'] is False


def test_missing_git_projection_rebuild_preserves_canonical_state(full,full_project):
    c=full;p=full_project[0];base=c.k.baseline(c.owner,p)
    snapshot=base['snapshot_blob'];before=c.k.export(c.owner,p)
    shutil.rmtree(c.s.home/'git'/('spec-'+p))
    checked=c.history.verify(c.owner,base['id'])
    assert checked['snapshot_verified'] and not checked['git_view_verified']
    rebuilt=c.history.rebuild_git(c.owner,base['id'],snapshot,'Git view was lost; reconstruct from captured state')
    assert rebuilt['git_view_verified'] and not rebuilt['replayed']
    assert c.k.export(c.owner,p)==before
    assert c.history.get(c.owner,base['id'])['snapshot_blob']==snapshot
    assert c.history.rebuild_git(c.owner,base['id'],snapshot,'Already repaired')['replayed']
    assert c.s.one("SELECT id FROM events WHERE kind='baseline_git_regenerated'")


def test_rebuilding_old_baseline_never_moves_current_backwards(full,full_project):
    c=full;p=full_project[0];old=c.k.baseline(c.owner,p)
    c.k.propose(c.owner,p,'design',{'title':'Added design','statement':'New state'})
    new=c.k.baseline(c.owner,p);bare=c.s.home/'git'/('spec-'+p)
    current=git(bare,'rev-parse','refs/heads/current').stdout.strip()
    git(bare,'update-ref','-d','refs/daikibo/baselines/'+old['id'])
    assert not c.history.verify(c.owner,old['id'])['git_view_verified']
    c.history.rebuild_git(c.owner,old['id'],old['snapshot_blob'],'Restore old reference only')
    assert git(bare,'rev-parse','refs/heads/current').stdout.strip()==current
    assert c.history.verify(c.owner,new['id'])['git_view_verified']


def test_wrong_snapshot_rejected_without_changing_state(full,full_project):
    c=full;base=c.k.baseline(c.owner,full_project[0])
    with pytest.raises(Fault) as exc:c.history.rebuild_git(c.owner,base['id'],'0'*64,'Wrong revision')
    assert exc.value.code=='stale_snapshot'
    assert c.history.get(c.owner,base['id'])['git_commit']==base['git_commit']


def test_corrupt_canonical_snapshot_never_regenerates_a_git_view(full,full_project):
    c=full;base=c.k.baseline(c.owner,full_project[0]);h=base['snapshot_blob']
    (c.s.blobs/h[:2]/h[2:]).write_bytes(b'corrupt')
    with pytest.raises(Fault) as exc:c.history.rebuild_git(c.owner,base['id'],h,'Cannot repair from corrupt source')
    assert exc.value.code=='integrity_error'


@pytest.mark.parametrize('mutation',['source_changed','missing_revision','missing_disposition_target','broken_document'])
def test_rechecks_semantic_record_hashes_not_only_zip_hash(full,full_project,tmp_path,mutation):
    c=full;p=full_project[0];design=add_revisions(c,p)
    c.documents.register(c.owner,p,base64.b64encode(b'original').decode(),'raw.bin')
    base=c.k.baseline(c.owner,p);export=c.history.export_archive(c.owner,base['id']);target=tmp_path/(mutation+'.zip')
    def alter(payload):
        spec=payload['specifications']
        if mutation=='source_changed':spec['source_contents'][next(iter(spec['source_contents']))]='different'
        if mutation=='missing_revision':spec['revisions']=[r for r in spec['revisions'] if not (r['artifact']==design and r['revision']==2)]
        if mutation=='missing_disposition_target':spec['dispositions'][0]['refs']=['not-an-artifact']
        if mutation=='broken_document':spec['document_contents'][next(iter(spec['document_contents']))]=base64.b64encode(b'different').decode()
    h=rewrite_archive(export['path'],target,alter)
    with pytest.raises(Fault):inspect_archive(target,h)


def test_missing_archive_member_rejected(full,full_project,tmp_path):
    c=full;base=c.k.baseline(c.owner,full_project[0]);export=c.history.export_archive(c.owner,base['id'])
    target=tmp_path/'missing.zip'
    with zipfile.ZipFile(export['path']) as old,zipfile.ZipFile(target,'w') as archive:
        archive.writestr('manifest.json',old.read('manifest.json'))
    with pytest.raises(Fault):inspect_archive(target,digest(target.read_bytes()))


def test_legacy_baseline_not_reconstructed_from_present_day_state(full,full_project):
    c=full;base=c.k.baseline(c.owner,full_project[0])
    # Simulate a genuine older DB whose baseline index had no self-contained snapshot.
    c.s.execute('DELETE FROM knowledge_snapshots WHERE baseline=?',(base['id'],))
    with pytest.raises(Fault) as exc:c.history.get(c.owner,base['id'])
    assert exc.value.code=='legacy_baseline'
    assert c.history.list(c.owner,full_project[0])['baselines'][0]['snapshot_blob'] is None


def test_full_backup_keeps_knowledge_archive_and_new_ledger(full,full_project,tmp_path):
    c=full;base=c.k.baseline(c.owner,full_project[0]);backup=c.ops.backup(c.owner)
    destination=tmp_path/'restore'
    restore_backup(backup['path'],destination,backup['sha256'])
    restored=Control(destination,mode='validation',start_workers=False)
    try:
        owner=Actor('local-user','owner')
        assert restored.history.verify(owner,base['id'])['git_view_verified']
        assert restored.history.get(owner,base['id'])['snapshot_blob']==base['snapshot_blob']
    finally:restored.close()


def test_inspect_baseline_cli_offline(full,full_project,capsys):
    from daikibo.cli import main
    c=full;base=c.k.baseline(c.owner,full_project[0]);export=c.history.export_archive(c.owner,base['id'])
    assert main(['inspect-baseline',export['path'],'--sha256',export['sha256']])==0
    assert json.loads(capsys.readouterr().out)['verified']


@pytest.mark.parametrize('mutation',['future_revision','duplicate_document','missing_change','accepted_summary','bad_structure'])
def test_archive_rejects_inconsistent_history_even_when_outer_hashes_are_recomputed(full,full_project,tmp_path,mutation):
    c=full;p=full_project[0];design=add_revisions(c,p)
    c.documents.register(c.owner,p,base64.b64encode(b'content').decode(),'document.bin')
    base=c.k.baseline(c.owner,p);export=c.history.export_archive(c.owner,base['id']);target=tmp_path/'invalid.zip'
    def alter(payload):
        spec=payload['specifications']
        if mutation=='future_revision':
            row=copy.deepcopy(next(r for r in spec['revisions'] if r['artifact']==design));row['revision']=100;spec['revisions'].append(row)
        if mutation=='duplicate_document':spec['documents'].append(copy.deepcopy(spec['documents'][0]))
        if mutation=='missing_change':spec['attempts'].append({'id':'ATT-dangling','change_id':'CHG-absent'})
        if mutation=='accepted_summary':payload['baseline']['artifacts']=[]
        if mutation=='bad_structure':spec['revisions']=None
    checksum=rewrite_archive(export['path'],target,alter)
    with pytest.raises(Fault):inspect_archive(target,checksum)


def test_interrupted_reexport_preserves_previous_complete_archive(full,full_project,monkeypatch):
    import daikibo.knowledge_history as history
    c=full;base=c.k.baseline(c.owner,full_project[0]);first=c.history.export_archive(c.owner,base['id'])
    path=Path(first['path']);original=path.read_bytes()
    def interrupted(*args,**kwargs):raise OSError('Simulated full disk before atomic rename')
    monkeypatch.setattr(history.os,'replace',interrupted)
    with pytest.raises(OSError):c.history.export_archive(c.owner,base['id'])
    assert path.read_bytes()==original
    assert not list(path.parent.glob('.spec-export-*.tmp'))
    assert inspect_archive(path,first['sha256'])['verified']
