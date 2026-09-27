import copy,json,os,sys
from pathlib import Path
import pytest
from daikibo.common import Fault,parse_json,digest
from daikibo.build_outputs import validate_definition,read_output
from conftest import make_task,finish_task
from test_delivery_git_and_recovery import profile


def built_profile(p,r,q,t):
    pr=profile(p,r,q,t)
    pr['build_outputs']=[{'id':'package','repo':r,'path':'.daikibo-build/app.bin'}]
    pr['checks'][0]['argv']=['python','-c','from pathlib import Path; import calc; Path(".daikibo-build/app.bin").write_text(str(calc.add(2,3)))']
    pr['checks'][0]['produces']=['package']
    pr['checks'][1]['argv']=['python','-c','from pathlib import Path; assert Path(".daikibo-build/app.bin").read_text()=="5"']
    pr['checks'][1]['uses']=['package']
    return pr


def test_build_outputs_are_observed_and_consumed_in_fresh_verification_workspace(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project)
    c.d.configure(c.owner,p,built_profile(p,r,q,t));finish_task(c,p,t)
    d=c.d.prepare(c.owner,p);checks=c.d.verify(c.owner,d['id'])
    assert all(r['passed'] for r in checks['results']),checks
    body=parse_json(c.s.one('SELECT body FROM deliveries WHERE id=?',(d['id'],))['body'])
    artifact=body['build_outputs']['package'];assert c.s.blob_get(artifact['blob'])==b'5'
    build=c.g.receipt(checks['results'][0]['receipt']);start=c.g.receipt(checks['results'][1]['receipt'])
    assert build['run']!=start['run'] and start['result']['build_inputs']==[artifact]
    assert artifact['producer_receipt']==build['id'] and not build['input_mutated']
    assert all('.daikibo-build' not in f for repo in body['snapshot']['repos'].values() for f in repo['files'])
    with pytest.raises(Fault):c.d.certify(c.owner,d['id'])  # fixture review is never a real release approval


def test_failed_build_never_fabricates_a_downstream_execution(full,full_project):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);pr=built_profile(p,r,q,t)
    pr['checks'][0]['argv']=['python','-c','raise SystemExit(1)']
    c.d.configure(c.owner,p,pr);finish_task(c,p,t);d=c.d.prepare(c.owner,p)
    checks=c.d.verify(c.owner,d['id'])['results']
    assert not checks[0]['passed'] and not checks[1]['passed'] and not checks[1].get('receipt')
    assert not c.s.one("SELECT id FROM runs WHERE subject=? AND role='delivery:start'",(d['id'],))


@pytest.mark.parametrize('change',['unconsumed','duplicate','wrong-path','before-build'])
def test_invalid_build_contract_rejected(full,full_project,change):
    c=full;p,r,q,_=full_project;t=make_task(c,full_project);pr=built_profile(p,r,q,t)
    if change=='unconsumed':pr['checks'][1].pop('uses')
    elif change=='duplicate':pr['build_outputs'].append(copy.deepcopy(pr['build_outputs'][0]))
    elif change=='wrong-path':pr['build_outputs'][0]['path']='calc.py'
    else:pr['checks'][0],pr['checks'][1]=pr['checks'][1],pr['checks'][0]
    with pytest.raises(Fault):c.d.configure(c.owner,p,pr)


def test_build_collector_rejects_symlinks_and_hardlinks(tmp_path):
    f=tmp_path/'file';f.write_text('artifact');link=tmp_path/'link';link.symlink_to(f)
    with pytest.raises(Fault):read_output(link)
    hard=tmp_path/'hard';os.link(f,hard)
    with pytest.raises(Fault):read_output(f)


def test_python_startup_uses_same_user_without_privileged_launcher(full,full_project):
    c=full;p,r,q,root=full_project
    (root/'sitecustomize.py').write_text('import os\nprint("STARTUP_UID="+str(os.geteuid()))\n')
    snap=c.sn.capture(c.owner,p)
    def command(work,home,cwd):
        # A target command may deliberately use its own module search path.
        # It must not affect the privileged launcher, whose interpreter uses -I -S.
        return [sys.executable,'-c','import os; print("TARGET_UID="+str(os.geteuid()))'],None
    result,_,_=c.rt.observe(p,None,q,'test-env',None,'b',snap,command,extra_env={'PYTHONPATH':'.'},readonly=True)
    output=c.s.blob_get(result['stdout_blob']).decode()
    assert 'STARTUP_UID='+str(result['worker_uid']) in output
    assert 'TARGET_UID='+str(result['worker_uid']) in output
