"""Large operational backups stream bytes; they are not portable spec certification."""
import io
import json
import zipfile
from pathlib import Path
import pytest
from daikibo.common import Fault,canonical
from daikibo.operations import restore_backup,file_hash,copy_hashed
from daikibo.control import Control
from test_reviewed_breakdowns import setup


def test_copy_bounded_size_and_actual_digest():
    class Bounded(io.BytesIO):
        def read(self,n=-1):
            assert 0<n<=1024*1024
            return super().read(n)
    data=b'abcd'*1_000_000;out=io.BytesIO()
    record=copy_hashed(Bounded(data),out)
    assert record['bytes']==len(data) and out.getvalue()==data
    with pytest.raises(Fault):copy_hashed(Bounded(data),io.BytesIO(),limit=len(data)-1)


def test_backup_restore_never_reads_entire_files_and_retains_open_upload(setup,tmp_path,monkeypatch):
    c,p,r,q,program,domain,task,units=setup
    upload=c.breakdown_inputs.begin(c.owner,program,'Resume after restore','Full future plan')
    c.breakdown_inputs.put(c.owner,upload['upload'],0,units[:1])
    blob=c.s.blob_put(b'big-history-content'*400_000)
    def reject(self):raise AssertionError('Whole-file allocation is forbidden in streaming backup/restore')
    with monkeypatch.context() as patch:
        patch.setattr(Path,'read_bytes',reject)
        backup=c.ops.backup(c.owner)
        restore_backup(backup['path'],tmp_path/'restored',backup['sha256'])
    restored=Control(tmp_path/'restored',mode='validation',start_workers=False)
    try:
        status=restored.breakdown_inputs.status(restored.sec.authenticate(),upload['upload'])
        assert status['revision']==1 and status['units']==1 and status['status']=='open'
        assert restored.s.blob_get(blob)==b'big-history-content'*400_000
    finally:restored.close()


@pytest.mark.parametrize('fault',['hash','size','member','database','duplicate'])
def test_stream_restore_rejects_corruption_without_publishing_destination(full,tmp_path,fault):
    backup=full.ops.backup(full.owner);path=tmp_path/'broken.zip'
    with zipfile.ZipFile(backup['path']) as source:
        content={name:source.read(name) for name in source.namelist()}
    manifest=json.loads(content['manifest.json'])
    if fault=='hash':manifest['files']['state.sqlite3']['sha256']='0'*64
    elif fault=='size':manifest['files']['state.sqlite3']['bytes']+=1
    elif fault=='member':content['extra-file']=b'extra'
    elif fault=='database':
        import hashlib
        content['state.sqlite3']=b'not a database';manifest['files']['state.sqlite3']={'sha256':hashlib.sha256(content['state.sqlite3']).hexdigest(),'bytes':len(content['state.sqlite3'])}
    content['manifest.json']=canonical(manifest)
    with zipfile.ZipFile(path,'w') as dest:
        for name,value in content.items():dest.writestr(name,value)
        if fault=='duplicate':
            with pytest.warns(UserWarning):dest.writestr('manifest.json',content['manifest.json'])
    import sqlite3
    with pytest.raises((Fault,sqlite3.DatabaseError)):
        restore_backup(path,tmp_path/'target',file_hash(path))
    assert not (tmp_path/'target').exists()


def test_failed_backup_not_published(full,monkeypatch):
    import daikibo.operations as ops
    def fail(*args,**kwargs):raise OSError('storage failed')
    monkeypatch.setattr(ops,'copy_hashed',fail)
    with pytest.raises(OSError):full.ops.backup(full.owner)
    assert not list((full.s.home/'exports').glob('*.zip'))
    assert not list((full.s.home/'exports').glob('*.partial'))
