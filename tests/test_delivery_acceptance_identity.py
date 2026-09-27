import json
import sys
import pytest
from daikibo.common import Fault, canonical, digest
from daikibo.obligations import delivery_coverage, marker
from test_reviewed_breakdowns import setup, accepted, make_work, leaf, propose, review_all
from test_delegated_workstreams import activate_scope, finish
from test_program_delivery_binding import prepared
from test_delivery_git_and_recovery import profile


def req(ident, *labels):return {'id':ident, 'body':{'acceptance':list(labels)}}


def test_unique_delivery_labels_preserve_old_coverage():
    result=delivery_coverage([req('A','one','two'),req('B','three')])
    assert set(result['required_coverage'])=={'one','two','three'}
    assert len(result['acceptance_identity'])==3
    assert not any(r['marker_required'] for r in result['acceptance_identity'])


@pytest.mark.parametrize('label',['AC-001','完了できる','a / b', 'reqac:ordinary'])
def test_ambiguous_delivery_labels_require_both_exact_pairs(label):
    result=delivery_coverage([req('A',label),req('B',label)])
    assert set(result['required_coverage'])=={label,marker('A',label),marker('B',label)}
    assert all(r['marker_required'] for r in result['acceptance_identity'])


def test_duplicate_requirement_and_marker_collision_rejected():
    with pytest.raises(Fault):delivery_coverage([req('A','a'),req('A','a')])
    with pytest.raises(Fault):delivery_coverage([req('A','a'),req('B','a'),req('C',marker('A','a'))])


def test_delivery_coverage_is_order_independent():
    assert delivery_coverage([req('A','one','two'),req('B','one')])==delivery_coverage([req('B','one'),req('A','two','one')])


def duplicate_delivery(s):
    c,p,r,q,program,d,t,units=s
    second=accepted(c,p,'requirement','A different outcome with same display label',acceptance=['AC-ADD'])
    t2=make_work(c,p,r,second,d)
    body=profile(p,r,q,t);body['required_requirements'].append(second);body['required_tasks'].append(t2);body['program']=program
    c.d.configure(c.owner,p,body)
    units=units+[leaf('other',d,[t2],second,parent='system')]
    b=propose(s,units);review_all(c,b['id']);c.breakdowns.activate(c.owner,b['id'])
    for task in (t,t2):finish(c,p,task)
    return c.d.prepare(c.owner,p)['id'],second


def test_context_and_gate_require_distinct_obligations(setup,tmp_path):
    c,p,r,q,*_=setup;delivery,second=duplicate_delivery(setup)
    _,binding,_,context,_=c.d.review_subject(c.owner,delivery)
    expected={'AC-ADD',marker(q,'AC-ADD'),marker(second,'AC-ADD')}
    assert set(context['required_coverage'])==expected
    script=tmp_path/'display_only_review.py'
    script.write_text("import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({'verdict':'pass','rationale':'Test fixture only','covered':['AC-ADD'],'findings':[],'observations':[{'ref':p['subject'],'detail':'Local test'}],'dispositions':[]}))\n")
    c.rt.adapters.register(c.owner,'labels-only','fixture',sys.executable,[str(script)])
    for role in ('integration','goal_validation'):c.rt.review(c.owner,delivery,role,'labels-only')
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert 'integration:review_coverage' in exc.value.details and 'goal_validation:review_coverage' in exc.value.details
    for role in ('integration','goal_validation'):c.rt.review(c.owner,delivery,role,'markers')
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    # The exact identity check passes; this is still a validation-mode fixture, not deploy readiness.
    assert 'integration:review_coverage' not in exc.value.details and 'goal_validation:review_coverage' not in exc.value.details
    assert 'validation_mode_cannot_certify_deploy_ready' in exc.value.details


def test_direct_certification_cannot_ignore_declared_unfinished_workstream(setup):
    c=setup[0];delivery,_=prepared(setup)
    scope=activate_scope(setup)
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert 'delegated_work_incomplete:'+setup[4] in exc.value.details
    c.workstreams.finish(c.owner,scope)
    with pytest.raises(Fault) as exc:c.d.certify(c.owner,delivery)
    assert not any(x.startswith('delegated_work_incomplete:') for x in exc.value.details)
    assert 'validation_mode_cannot_certify_deploy_ready' in exc.value.details


def test_delivery_reviewer_can_recover_exact_frozen_baseline(setup):
    import base64
    c,p,r,*_=setup
    original=(setup[0].s.one('SELECT path FROM repos WHERE id=?',(r,),True)['path'])
    from pathlib import Path
    before=(Path(original)/'test_calc.py').read_bytes()
    delivery,_=prepared(setup)
    # Reading the real repository later must not change the frozen review baseline.
    (Path(original)/'test_calc.py').write_text('changed after snapshot capture\n')
    _,_,_,context,_=c.d.review_subject(c.owner,delivery)
    encoded=c.blob_read(c.owner,context['baseline']['snapshot_blob'],p)
    baseline=json.loads(base64.b64decode(encoded['base64']))
    assert baseline['digest']==context['baseline']['snapshot_digest']
    blob=baseline['repos'][r]['files']['test_calc.py']['blob']
    data=c.blob_read(c.owner,blob,p)
    assert base64.b64decode(data['base64'])==before
