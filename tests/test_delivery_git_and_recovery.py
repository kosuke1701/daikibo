import copy
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest
from daikibo.common import Fault,canonical,digest,parse_json,timestamp
from daikibo.gitops import git
from daikibo.operations import restore_backup
from daikibo.control import Control
from conftest import make_task,finish_task


def profile(p,r,q,t):
    return {'target_environment':'CPython 3.13 Linux, no services','required_requirements':[q],'required_tasks':[t],'repo_order':[r],
            'rollback':'Restore previously verified Git commit; no persistent user data in this fixture',
            'applicability':{k:{'applicable':False,'reason':'No '+k+' surface in the isolated arithmetic test fixture'} for k in ('migration','security','performance','contract')},
            'checks':[{'id':'build','category':'build','repo':r,'kind':'command','argv':['python','-m','compileall','-q','.'],'purpose':'Compile all source'},
                      {'id':'start','category':'start','repo':r,'kind':'command','argv':['python','-c','import calc; print(calc.add(2,3))'],'purpose':'Module starts in a clean process'},
                      *[{'id':kind,'category':kind,'repo':r,'kind':'pytest','argv':['python','-m','pytest','-q','test_calc.py'],'required_tests':['test_add']} for kind in ('smoke','integration','scenario')]]}


def test_full_snapshot_integration_tests_run_but_validation_never_certifies(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project)
    c.d.configure(c.owner,p,profile(p,r,q,t));finish_task(c,p,t)
    delivery=c.d.prepare(c.owner,p);checks=c.d.verify(c.owner,delivery['id'])
    assert all(x['passed'] for x in checks['results']),checks
    for role in ('integration','goal_validation'):c.rt.review(c.owner,delivery['id'],role,'fixture')
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery['id'])
    assert exc.value.code=='release_gate_denied'
    assert 'validation_mode_cannot_certify_deploy_ready' in exc.value.details
    with pytest.raises(Fault):c.d.commit(c.owner,delivery['id'],'must not commit unqualified delivery')


def test_frozen_scope_cannot_remove_work(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);pr=profile(p,r,q,t)
    frozen=c.d.configure(c.owner,p,pr);removed=copy.deepcopy(pr);removed['required_tasks']=[]
    with pytest.raises(Fault):c.d.configure(c.owner,p,removed,expected_digest=frozen['digest'])
    extra=make_task(c,full_project,paths=['other.py'])
    finish_task(c,p,t)
    with pytest.raises(Fault,match='task set does not match'):c.d.prepare(c.owner,p)


def test_exact_git_snapshot_and_baseline_view(full,full_project):
    c=full;p,r,q,root=full_project
    snapshot=c.sn.capture(c.owner,p);committed=c.sn.commit_snapshot(snapshot,r,'Snapshot test')
    observed=git(Path(committed['git_dir']),'show',committed['commit']+':calc.py').stdout
    assert observed==root.joinpath('calc.py').read_bytes()
    again=c.sn.commit_snapshot(snapshot,r,'idempotent repeat')
    assert again['commit']==committed['commit']
    b=c.k.baseline(c.owner,p)
    assert b['git_commit'] and c.s.one('SELECT git_commit FROM baselines WHERE id=?',(b['id'],))['git_commit']==b['git_commit']


def test_snapshot_excludes_git_metadata_files_and_directories(system,tmp_path):
    s=system
    project=s.k.create_project(s.owner,'Git worktree snapshot')['id']
    repository=tmp_path/'repository';repository.mkdir()
    (repository/'tracked.py').write_text('tracked = True\n')
    (repository/'.gitignore').write_text('*.pyc\n')
    git(repository,'init');git(repository,'add','tracked.py','.gitignore');git(repository,'commit','-m','Initial source')
    worktree=tmp_path/'worktree'
    git(repository,'worktree','add','-b','feature',str(worktree))
    (worktree/'untracked.py').write_text('untracked = True\n')

    repository_id=s.sn.register(s.owner,project,'repository',str(repository))['id']
    worktree_id=s.sn.register(s.owner,project,'worktree',str(worktree))['id']
    snapshot=s.sn.capture(s.owner,project)
    repository_files=snapshot['repos'][repository_id]['files']
    worktree_files=snapshot['repos'][worktree_id]['files']
    assert (repository/'.git').is_dir() and (worktree/'.git').is_file()
    assert '.git' not in repository_files and '.git' not in worktree_files
    assert {'.gitignore','tracked.py'} <= repository_files.keys()
    assert {'tracked.py','untracked.py'} <= worktree_files.keys()

    destination=tmp_path/'materialized'
    s.sn.materialize(snapshot,destination)
    assert not (destination/'repository'/'.git').exists()
    assert not (destination/'worktree'/'.git').exists()
    assert (destination/'repository'/'.gitignore').read_text()=='*.pyc\n'
    assert (destination/'worktree'/'tracked.py').read_text()=='tracked = True\n'
    (destination/'worktree'/'edited.py').write_text('edited = True\n')
    after=s.sn.collect(snapshot,destination)
    assert [(change['repo'],change['path']) for change in s.sn.changes(snapshot,after)] == [(worktree_id,'edited.py')]


def test_initial_git_history_is_retained(full,full_project):
    c=full;p,r,q,root=full_project
    git(root,'init');git(root,'add','.');git(root,'commit','-m','Original source');head=git(root,'rev-parse','HEAD').stdout.decode().strip()
    (root/'calc.py').write_text('def add(a,b):\n    return a+b\n')
    snapshot=c.sn.capture(c.owner,p)
    committed=c.sn.commit_snapshot(snapshot,r,'Managed update')
    parent=git(Path(committed['git_dir']),'rev-parse',committed['commit']+'^').stdout.decode().strip()
    assert parent==head


def test_dependency_candidate_is_in_next_task_input(full,full_project):
    c=full;p=full_project[0];a=make_task(c,full_project);finish_task(c,p,a)
    b=make_task(c,full_project,goal='WRITE:'+json.dumps({'readme.txt':'addition complete\n'}),paths=['readme.txt'],deps=[a])
    c.w.claim(c.owner,p,b);c.rt.execute(c.owner,b,'fixture')
    checks=c.rt.tests(c.owner,b)
    assert checks['checks'][0]['result']['passed'],'Dependent task must see completed candidate, not stale source checkout'


def test_compatible_disjoint_candidates_merge_conflicting_ones_do_not(full,full_project):
    c=full;p,r,q,_=full_project;a=make_task(c,full_project);finish_task(c,p,a)
    b=make_task(c,full_project,goal='WRITE:'+json.dumps({'calc.py':'def add(a,b):\n    return sum([a,b])\n'}));finish_task(c,p,b)
    with pytest.raises(Fault,match='incompatibly'):c.d.assemble(c.owner,p,[a,b])


def test_backup_restore_and_old_tokens_are_revoked(full,full_project,tmp_path):
    c=full;p=full_project[0];oldtoken=Path(c.sec.bootstrap()).read_text()
    backup=c.ops.backup(c.owner)
    target=tmp_path/'restored'
    result=restore_backup(backup['path'],target,backup['sha256']);assert result['restored']
    restored=Control(target,mode='validation',start_workers=False)
    try:
        owner=restored.sec.authenticate(Path(restored.sec.bootstrap()).read_text())
        assert restored.k.project(owner,p)['id']==p
        assert restored.sec.audit()['verified']
        assert restored.sec.authenticate().role == 'owner'
        assert not (target/'owner.token').exists()
    finally:restored.close()
    with pytest.raises(Fault):restore_backup(backup['path'],tmp_path/'bad','0'*64)


def test_restart_marks_interrupted_run_unknown_not_passed(full,tmp_path):
    c=full;p=c.k.create_project(c.owner,'recover')['id']
    # Deliberately simulate a crash at the persisted run-registration boundary, not a completed receipt.
    c.s.execute("INSERT INTO runs(id,project,subject,role,adapter,status,binding,start,body) VALUES(?,?,?,?,?,'running',?,?,?)",('RUN-interrupted',p,'subject','implementer','missing','binding',timestamp(),'{}'))
    c.ops.reconcile_startup()
    assert c.s.one('SELECT status FROM runs WHERE id=?',('RUN-interrupted',))['status']=='unknown'
    assert c.s.one('SELECT id FROM inbox WHERE ref=?',('RUN-interrupted',))
    with pytest.raises(Fault):c.g.receipt('RUN-interrupted')


def test_backup_archive_extra_file_rejected(full,tmp_path):
    c=full;b=c.ops.backup(c.owner);path=tmp_path/'modified.zip';path.write_bytes(Path(b['path']).read_bytes())
    with zipfile.ZipFile(path,'a') as z:z.writestr('../outside','evil')
    with pytest.raises(Fault):restore_backup(path,tmp_path/'unsafe',digest(path.read_bytes()))
