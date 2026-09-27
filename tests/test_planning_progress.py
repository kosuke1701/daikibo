"""New observations permit bounded reasoning turns; rereading must not loop."""
import json
import sys
import pytest
from daikibo.common import canonical, parse_json
from test_reviewed_breakdowns import setup


def planner(c,tmp_path,source):
    script=tmp_path/'reading_planner.py'
    script.write_text('import json,sys\np=json.load(sys.stdin)\n'+
        'print(json.dumps('+repr({'message':'Fixture planner reads one exact source range','actions':[{'method':'source.read','params':{'source':source,'start':0,'limit':8}}],'questions':[]})+'))\n')
    c.rt.adapters.register(c.owner,'reading','fixture',sys.executable,[str(script)])
    return 'reading'


def test_first_read_is_progress_identical_reads_are_not_infinite_progress(setup,tmp_path):
    c,p,r,q,program,d,t,units=setup
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id'];adapter=planner(c,tmp_path,source)
    engineering=c.supervisor.state_digest(p);before=c.supervisor.progress_digest(p)
    result=c.supervisor.turn(c.owner,p,adapter)
    assert result['actions'][0]['result']['content']
    assert c.supervisor.state_digest(p)==engineering
    after=c.supervisor.progress_digest(p);assert after!=before
    c.supervisor.turn(c.owner,p,adapter)
    assert c.supervisor.progress_digest(p)==after
    assert c.s.one('SELECT count(*) AS n FROM supervisor_views')['n']==1
    assert c.s.one("SELECT count(*) AS n FROM receipts WHERE role='supervisor'")['n']==2


def test_completed_external_review_advances_progress_but_not_engineering_state(setup):
    c,p,r,q,program,d,t,units=setup
    engineering=c.supervisor.state_digest(p);before=c.supervisor.progress_digest(p)
    c.rt.review(c.owner,q,'requirements','markers')
    assert c.supervisor.progress_digest(p)!=before
    assert c.supervisor.state_digest(p)==engineering


def test_planner_can_continue_after_read_only_turn_then_stops_duplicate_reads(setup,tmp_path):
    c,p,r,q,program,d,t,units=setup
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id'];adapter=planner(c,tmp_path,source)
    c.jobs.configure(c.owner,p,adapter,'markers',concurrency=1)
    c.jobs._automate()
    job=c.s.one("SELECT * FROM jobs WHERE project=? AND kind='supervisor.turn' AND status='queued'",(p,),True)
    assert c.jobs.run_one(job)['status']=='succeeded'
    c.jobs._automate()
    job=c.s.one("SELECT * FROM jobs WHERE project=? AND kind='supervisor.turn' AND status='queued'",(p,),True)
    assert c.jobs.run_one(job)['status']=='succeeded'
    c.jobs._automate()
    assert not c.s.one("SELECT * FROM jobs WHERE project=? AND kind='supervisor.turn' AND status='queued'",(p,))
    assert c.s.one("SELECT count(*) AS n FROM jobs WHERE kind='supervisor.turn'")['n']==2


def test_observation_clock_and_id_do_not_fake_discovery_progress(setup):
    c,p,r,q,program,d,t,units=setup
    ev=c.rt.review(c.owner,q,'requirements','markers')['receipt']
    params={'project':p,'query':'add'};result={'results':[{'symbol':'add'}],'observation':'first','now':1}
    c.supervisor.record_view(p,'code.search',params,result,ev);after=c.supervisor.progress_digest(p)
    c.supervisor.record_view(p,'code.search',params,{**result,'observation':'second','now':2},ev)
    assert c.supervisor.progress_digest(p)==after
    c.supervisor.record_view(p,'code.search',params,{'results':[{'symbol':'add_v2'}]},ev)
    assert c.supervisor.progress_digest(p)!=after


def test_uninteresting_status_poll_is_not_recorded_as_reasoning_progress(setup):
    c,p,r,q,program,d,t,units=setup
    ev=c.rt.review(c.owner,q,'requirements','markers')['receipt'];before=c.supervisor.progress_digest(p)
    c.supervisor.record_view(p,'workflow.status',{'project':p},{'now':1},ev)
    assert c.supervisor.progress_digest(p)==before


def test_view_cursor_survives_restart(setup,tmp_path):
    from daikibo.control import Control
    c,p,r,q,program,d,t,units=setup;home=c.s.home
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id'];adapter=planner(c,tmp_path,source)
    c.supervisor.turn(c.owner,p,adapter);progress=c.supervisor.progress_digest(p);c.close()
    other=Control(home,mode='validation',start_workers=False)
    try:assert other.supervisor.progress_digest(p)==progress
    finally:other.close()
