import json
import sys

from daikibo.qualification import catalog


def test_qualification_prompts_require_exact_acceptance_coverage(full,monkeypatch):
    c=full
    project=c.k.create_project(c.owner,'qualification contract')['id']
    c.rt.adapters.register(c.owner,'mock-live','codex',sys.executable)
    cases=catalog()['cases']
    expected={case['id']:case['expect'] for case in cases}
    observed=[];phase='missing'

    def observe(project_id,task,subject,role,adapter,binding,snapshot,argv_factory,prompt,**kwargs):
        body=json.loads(prompt)
        observed.append(body)
        name=body['acceptance'][0].removeprefix('AC-QUALIFY-')
        covered=[] if phase=='missing' and name=='test-correct' else body['acceptance']
        result={'verdict':expected[name],'rationale':'Mocked observe contract test',
                'covered':covered,'findings':[],'observations':[{'ref':'fixture','detail':'Mocked observe only'}],'dispositions':[]}
        return ({'id':'RECEIPT-'+str(len(observed)),'result':result,'judgment_valid':True,
                 'readonly_verified':True,'exit_code':0,'assurance':'governed'},None,None)

    monkeypatch.setattr(c.rt,'observe',observe)
    first=c.supervisor.qualify(c.owner,project,'mock-live')
    assert not first['qualified']
    assert first['cases'][3]['case']=='test-correct' and not first['cases'][3]['passed']
    assert len(observed)==len(cases)==8

    required_fragments=(
        'covered only to the exact ID strings from the acceptance array',
        'never substitute a formula, display name, or other label for an ID',
        'dispositions does not satisfy covered',
        'return blocked or fail as appropriate',
        'never fabricate a PASS verdict or acceptance ID',
    )
    for body,case in zip(observed,cases):
        assert body['acceptance']==['AC-QUALIFY-'+case['id']]
        assert all(fragment in body['instructions'] for fragment in required_fragments)

    phase='correct'
    second=c.supervisor.qualify(c.owner,project,'mock-live')
    assert second['qualified']
    assert all(item['passed'] for item in second['cases'])
    assert len(observed)==16
