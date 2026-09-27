"""Phase review inputs retain source text and classifications, including fragments."""
import json
import pytest
from daikibo.common import Fault


def test_whole_phase_reviewer_receives_exact_source_classifications_and_observations(full, full_project):
    c=full;project=full_project[0]
    source=c.s.one('SELECT id FROM sources WHERE project=?',(project,))['id']
    flow=c.p.begin(c.owner,project,source)['program']
    observed=c.rt.review(c.owner,full_project[2],'requirements','fixture')
    _,_,snapshot,material,_=c.rt._subject(c.owner,flow,'phase')
    assert not snapshot['repos']  # This is before implementation.
    item=next(x for x in material['sources'] if x['id']==source)
    assert item['body']['content']==c.k.source_read(c.owner,source)['content']
    assert item['body']['dispositions'][0]['refs']==[full_project[2]]
    evidence=next(x for x in material['observed_evidence'] if x['id']==observed['receipt'])
    assert evidence['binding']==c.k.artifact(c.owner,full_project[2])['digest']
    assert evidence['result']==observed['result']
    typed=next(x for x in material['typed_observations'] if x['id']==observed['receipt'])
    assert typed['subject_kind']=='artifact'
    assert typed['resolver']=='Knowledge.artifact'
    assert typed['observation_state']=='current'
    assert typed['subject_ref']['id']==full_project[2]
    assert all(item['kind'] in {'accepted_requirement','source_coverage'}
               for item in material['required_current_proof'])
    assert any(item['kind']=='source_coverage' and item['structurally_complete']
               for item in material['required_current_proof'])


def test_large_raw_source_is_reassembled_and_new_classification_invalidates_packets(full, full_project):
    c=full;project=full_project[0]
    text='長い原文と境界条件😀。\n'*1200
    source=c.k.source(c.owner,project,text)['id']
    flow=c.p.begin(c.owner,project,source)['program']
    parts=c.scopes.partition(c.owner,flow,10000)
    fragments=[];source_packets=[]
    for packet in parts['packets']:
        row=c.scopes.current(c.owner,packet['id'])
        items=[x for x in row['body']['items'] if x['type']=='source' and x['id']==source]
        if items:source_packets.append(packet['id']);fragments.extend(items)
        c.rt.review(c.owner,packet['id'],'phase','fixture')
    assert len(fragments)>1
    serialized=''.join(x['body']['serialized_fragment'] for x in sorted(fragments,key=lambda x:x['fragment']['index']))
    assert json.loads(serialized)=={'content':text,'dispositions':[]}
    summary=c.scopes.summary(c.owner,flow)
    assert summary['complete']
    assert all(x['result']['observations'] for x in summary['packets'])
    c.k.classify(c.owner,source,0,len(text),'reference',[],'New interpretation of the original material')
    for packet in source_packets:
        with pytest.raises(Fault,match='Source classification changed'):
            c.scopes.current(c.owner,packet)
    assert not c.scopes.summary(c.owner,flow)['complete']


def test_oversized_direct_phase_input_requires_partition_without_truncation(full, full_project):
    c=full;project=full_project[0]
    source=c.k.source(c.owner,project,'x'*900001)['id']
    flow=c.p.begin(c.owner,project,source)['program']
    with pytest.raises(Fault) as exc:c.rt._subject(c.owner,flow,'phase')
    assert exc.value.code=='context_insufficient'
    assert 'partition_review' in exc.value.message


def test_phase_typed_projection_keeps_stale_foreign_and_unknown_observations_separate(full, full_project):
    c=full;project=full_project[0]
    source=c.s.one('SELECT id FROM sources WHERE project=?',(project,))['id']
    flow=c.p.begin(c.owner,project,source)['program']
    row=c.s.one('SELECT * FROM programs WHERE id=?',(flow,))
    current=c.rt.review(c.owner,full_project[2],'requirements','fixture')
    _,_,_,material,_=c.rt._subject(c.owner,flow,'phase')
    observed=next(x for x in material['typed_observations'] if x['id']==current['receipt'])
    stale=c.rt._phase_observation(c.owner,row,{**observed,'binding':'0'*64})
    unknown=c.rt._phase_observation(c.owner,row,{**observed,'subject':'historical-BPACK','binding':'f'*64})
    corrupt=c.rt._phase_observation(c.owner,row,{**observed,'subject':None})
    assert stale['observation_state']=='historical' and stale['current'] is False
    assert unknown['observation_state']=='unknown' and unknown['current'] is False
    assert corrupt['observation_state']=='corrupt' and corrupt['current'] is False

    foreign_project=c.k.create_project(c.owner,'Foreign phase observation')['id']
    foreign=c.k.propose(c.owner,foreign_project,'requirement',
                        {'title':'Foreign','statement':'Other project material',
                         'acceptance':['AC-FOREIGN']})['id']
    foreign_row=c.s.one('SELECT digest FROM artifacts WHERE id=?',(foreign,))
    foreign_observation=c.rt._phase_observation(c.owner,row,{**observed,
        'subject':foreign,'binding':foreign_row['digest']})
    assert foreign_observation['observation_state']=='foreign'
