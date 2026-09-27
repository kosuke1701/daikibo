"""Portable byte/record validation, not live semantic acceptance or runtime restore."""
from __future__ import annotations
import base64
import copy
import hashlib
import shutil
import zipfile
from pathlib import Path
import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo import archive_chunks as chunks
from daikibo.knowledge_history import inspect_archive
from test_knowledge_history import add_revisions
from test_reviewed_breakdowns import setup, propose


def export(c,p,**kwargs):
    baseline=c.k.baseline(c.owner,p,layout='chunked',chunk_bytes=1024,**kwargs)
    return baseline,c.history.export_archive(c.owner,baseline['id'])


def rewrite(source,target,mutate):
    """Update all checksums to exercise relational validation, not only hashing."""
    with zipfile.ZipFile(source) as z:
        payload=parse_json(z.read('snapshot.json'));header=parse_json(z.read('manifest.json'))
        objects={h:z.read('objects/'+h) for h in payload['objects']}
    old_record_hashes={r['sha256'] for r in payload['records']['chunks']}
    raw=b''.join(objects[r['sha256']] for r in payload['records']['chunks'])
    rows=[parse_json(line,limit=chunks.MAX_RECORD_BYTES) for line in raw.splitlines()]
    mutate(payload,rows,objects)
    data=b''.join(canonical(row)+b'\n' for row in rows)
    # No raw-data chunk overlaps record-stream chunks in these fixtures.
    for h in old_record_hashes:objects.pop(h,None)
    parts=[]
    for offset in range(0,len(data),1024):
        block=data[offset:offset+1024];h=digest(block);objects[h]=block;parts.append({'sha256':h,'bytes':len(block)})
    payload['records']={'chunks':parts,'bytes':len(data),'sha256':digest(data),
                        'counts':{s:sum(r['section']==s for r in rows) for s in chunks.SECTIONS}}
    payload['objects']={h:{'bytes':len(b)} for h,b in objects.items()}
    payload['baseline_digest']=digest(payload['baseline'])
    root=canonical(payload);header['snapshot']={'sha256':digest(root),'bytes':len(root)}
    with zipfile.ZipFile(target,'w') as z:
        z.writestr('manifest.json',canonical(header));z.writestr('snapshot.json',root)
        for h,data in objects.items():z.writestr('objects/'+h,data)
    return chunks.file_digest(target)


def test_chunked_archive_preserves_multibyte_raw_revisions_and_uninterpreted_attachment(full,full_project,tmp_path):
    c=full;p=full_project[0];add_revisions(c,p)
    content='境界をまたぐ元の仕様。\U0001f680\n'*800
    c.k.source(c.owner,p,content)
    raw=bytes(range(256))*29
    c.documents.register(c.owner,p,base64.b64encode(raw).decode(),'raw.bin')
    baseline,archive=export(c,p)
    assert baseline['layout']=='chunked'
    result=inspect_archive(archive['path'],archive['sha256'])
    assert result['counts']['sources']==2 and result['counts']['documents']==1 and result['counts']['revisions']==4
    assert not result['runtime_restore_supported'] and not result['new_test_or_review_evidence']
    with zipfile.ZipFile(archive['path']) as z:
        manifest=parse_json(z.read('snapshot.json'));assert max(r['bytes'] for r in manifest['objects'].values())<=1024
    assert c.history.verify(c.owner,baseline['id'])['git_view_verified']


def test_chunked_portable_archive_survives_original_db_and_git_deletion(full,full_project,tmp_path):
    c=full;b,a=export(c,full_project[0]);path=tmp_path/'standalone.zip';shutil.copyfile(a['path'],path)
    home=c.s.home;c.close();shutil.rmtree(home)
    assert inspect_archive(path,a['sha256'])['verified']


def test_auto_selects_chunks_before_building_giant_json(full,full_project):
    c=full;p=full_project[0];c.k.source(c.owner,p,'x'*4_300_000)
    value=c.k.baseline(c.owner,p)
    assert value['layout']=='chunked'
    assert len(c.s.blob_get(value['snapshot_blob']))<65536


def test_chunked_old_baseline_is_immutable_and_git_can_be_rebuilt(full,full_project):
    c=full;p=full_project[0];b,a=export(c,p)
    c.k.source(c.owner,p,'A later request is not backdated into the old snapshot.')
    again=c.history.export_archive(c.owner,b['id']);assert a['sha256']==again['sha256']
    shutil.rmtree(c.s.home/'git'/('spec-'+p))
    assert not c.history.verify(c.owner,b['id'])['git_view_verified']
    result=c.history.rebuild_git(c.owner,b['id'],b['snapshot_blob'],'Recreate generated view without new approval')
    assert result['git_view_verified']
    assert c.history.get(c.owner,b['id'])['counts']['sources']==1


@pytest.mark.parametrize('fault',['missing_revision','changed_revision','missing_source','wrong_project','dangling_link','overlap','accepted_omitted','raw_order','character_count','duplicate_record','fake_evidence'])
def test_checksums_alone_cannot_mask_inconsistent_history(full,full_project,tmp_path,fault):
    c=full;p=full_project[0];design=add_revisions(c,p)
    c.k.source(c.owner,p,''.join(chr(97+i)*1000 for i in range(5)))
    b,a=export(c,p)
    def mutate(manifest,rows,objects):
        if fault=='missing_revision':rows.remove(next(r for r in rows if r['section']=='revisions' and r['row']['artifact']==design and r['row']['revision']==2))
        elif fault=='changed_revision':next(r for r in rows if r['section']=='revisions')['row']['body']['statement']='changed'
        elif fault=='missing_source':rows[:]=[r for r in rows if not(r['section']=='sources' and r['row']['characters']<100)]
        elif fault=='wrong_project':next(r for r in rows if r['section']=='artifacts')['row']['project']='other'
        elif fault=='dangling_link':rows.append({'section':'links','row':{'source':design,'target':'absent','relation':'realizes','confidence':'asserted','basis':'unrecorded'}})
        elif fault=='overlap':
            r=copy.deepcopy(next(r for r in rows if r['section']=='dispositions'));r['row']['id']='duplicate-interval';rows.append(r)
        elif fault=='accepted_omitted':manifest['baseline']['artifacts']=[]
        elif fault=='raw_order':next(r for r in rows if r['section']=='sources' and r['row']['characters']>100)['raw']['chunks'].reverse()
        elif fault=='character_count':next(r for r in rows if r['section']=='sources')['row']['characters']+=1
        elif fault=='duplicate_record':rows.append(copy.deepcopy(next(r for r in rows if r['section']=='artifacts')))
        elif fault=='fake_evidence':manifest['fresh_test_or_review_evidence']=True
    h=rewrite(a['path'],tmp_path/'bad.zip',mutate)
    with pytest.raises(Fault):inspect_archive(tmp_path/'bad.zip',h)


@pytest.mark.parametrize('fault',['missing_object','extra_object','duplicate_zip','truncated_stream','wrong_archive_hash'])
def test_archive_transport_and_manifest_corruption_rejected(full,full_project,tmp_path,fault):
    b,a=export(full,full_project[0]);target=tmp_path/'bad.zip'
    with zipfile.ZipFile(a['path']) as old:
        data={n:old.read(n) for n in old.namelist()}
    name=next(n for n in data if n.startswith('objects/'))
    if fault=='missing_object':del data[name]
    elif fault=='extra_object':data['objects/'+'0'*64]=b'not referenced'
    elif fault=='truncated_stream':data[name]=data[name][:-1]
    with zipfile.ZipFile(target,'w') as z:
        for n,v in data.items():z.writestr(n,v)
        if fault=='duplicate_zip':
            with pytest.warns(UserWarning):z.writestr(name,data[name])
    h='0'*64 if fault=='wrong_archive_hash' else chunks.file_digest(target)
    with pytest.raises(Fault):inspect_archive(target,h)


def test_staged_and_reviewed_plan_history_remains_in_archive(setup):
    c,p,r,q,program,d,t,units=setup
    upload=c.breakdown_inputs.begin(c.owner,program,'Archive plan','Preserve staging history')
    c.breakdown_inputs.put(c.owner,upload['upload'],0,units)
    plan=c.breakdown_inputs.finalize(c.owner,upload['upload'],1)
    b,a=export(c,p);result=inspect_archive(a['path'],a['sha256'])
    assert result['counts']['uploads']==1 and result['counts']['breakdowns']==1 and result['counts']['packets']>0


def test_missing_review_fragment_detected_after_all_hashes_updated(setup,tmp_path):
    c,p,*_=setup;propose(setup);b,a=export(c,p)
    def mutate(manifest,rows,objects):rows.remove(next(r for r in rows if r['section']=='members'))
    h=rewrite(a['path'],tmp_path/'bad-plan.zip',mutate)
    with pytest.raises(Fault):inspect_archive(tmp_path/'bad-plan.zip',h)


def test_stream_blob_ingestion_works_without_read_bytes(full,tmp_path,monkeypatch):
    path=tmp_path/'data';path.write_bytes(b'x'*2_000_000)
    expected=chunks.file_digest(path)
    def reject(*a,**k):raise AssertionError('whole-file read forbidden here')
    monkeypatch.setattr(Path,'read_bytes',reject)
    assert full.s.blob_put_file(path)==expected
    assert full.s.blob_put_file(path)==expected


def test_chunked_export_atomic_failure_preserves_prior_complete_zip(full,full_project,monkeypatch):
    c=full;b,a=export(c,full_project[0]);original=Path(a['path']).read_bytes()
    def fail(*a):raise OSError('simulated rename failure')
    monkeypatch.setattr(chunks.os,'replace',fail)
    with pytest.raises(OSError):c.history.export_archive(c.owner,b['id'])
    assert Path(a['path']).read_bytes()==original
    assert not list(Path(a['path']).parent.glob('.chunk-export-*'))


def test_legacy_export_rejects_pinless_unfinished_staging(full,full_project):
    c=full;p=full_project[0]
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    program=c.p.begin(c.owner,p,source)['program']
    upload=c.breakdown_inputs.begin(c.owner,program,'Staging only','Retain the unfinished upload')
    c.breakdown_inputs.put(c.owner,upload['upload'],0,[{
        'id':'staging-only-unit','title':'unfinished unit',
    }])
    baseline=c.k.baseline(c.owner,p)
    assert baseline['layout']=='chunked'
    result=c.history.get(c.owner,baseline['id'])
    assert result['counts']['uploads']==1 and result['counts']['upload_units']==1
    with pytest.raises(Fault,match='Legacy export cannot retain staged plans') as exc:
        c.k.baseline(c.owner,p,layout='legacy')
    assert exc.value.code=='legacy_cannot_preserve_staging'


def test_small_default_snapshot_keeps_unfinished_staging_with_assurance_history(setup):
    c,p,r,q,program,domain,task,units=setup
    upload=c.breakdown_inputs.begin(c.owner,program,'Saved staging','Partly submitted plan')
    c.breakdown_inputs.put(c.owner,upload['upload'],0,units[:1])
    baseline=c.k.baseline(c.owner,p)
    assert baseline['layout']=='chunked'
    result=c.history.get(c.owner,baseline['id'])
    assert result['counts']['uploads']==1 and result['counts']['upload_units']==1
    # The staged upload is represented in the traceability population, so
    # that guard intentionally runs before the later assurance-history guard.
    with pytest.raises(Fault,match='Use auto or chunked to retain traceability pins/staging') as exc:
        c.k.baseline(c.owner,p,layout='legacy')
    assert exc.value.code=='legacy_cannot_preserve_traceability'


def test_legacy_export_keeps_assurance_only_refusal_when_traceability_is_absent(full):
    from test_e3_selection_contract import _adopt, _fixture, _profile_body, _register_fixture_review

    project, _source, _requirement, program, scope = _fixture(full)
    _register_fixture_review(full)
    proposal = full.assurance.profile_propose(
        full.owner, project, program, _profile_body(project, program, scope), None,
    )
    _adopt(full, project, proposal, None)
    with pytest.raises(Fault, match='Use auto or chunked to retain immutable assurance history') as exc:
        full.k.baseline(full.owner, project, layout='legacy')
    assert exc.value.code == 'legacy_cannot_preserve_assurance'
