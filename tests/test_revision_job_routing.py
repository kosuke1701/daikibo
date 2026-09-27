"""A proposal review must work via the job route, not only direct Runtime calls."""
from test_task_definition_revisions import revision_setup, propose


def test_task_revision_impact_review_runs_via_managed_job(revision_setup):
    c,project,t=revision_setup;p=propose(c,t)
    args={'subject':p['id'],'role':'impact','adapter':'revision-reviewer'}
    assert c.jobs.subject_project('review',args)==project[0]
    job=c.jobs.submit(c.owner,'review',args)
    row=c.s.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
    outcome=c.jobs.run_one(row)
    result=outcome['result']
    assert c.jobs.get(c.owner,job['id'])['status']=='succeeded'
    assert result['result']['verdict']=='pass'
    applied=c.task_revisions.apply(c.owner,p['id'],p['digest'],result['receipt'])
    assert applied['revision']==2
