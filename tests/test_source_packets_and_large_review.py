"""Lossless context splitting with explicit mechanical/semantic distinction."""
import json
import pytest
from daikibo.common import Fault, canonical, digest, parse_json
from daikibo.packets import slices


@pytest.mark.parametrize('text_value',['hello\n'*500, '要件😀を保持します。\n'*500, 'x'*5000, 'é\u0301\n'*300, '\n'*1024])
@pytest.mark.parametrize('budget',[256,512,2048])
def test_utf8_segmentation_preserves_every_character_and_budget(text_value,budget):
    result=list(slices(text_value,budget))
    assert ''.join(p[2] for p in result)==text_value
    assert result[0][0]==0 and result[-1][1]==len(text_value)
    assert all(len(value.encode())<=budget for _,_,value in result)
    assert all(a[1]==b[0] for a,b in zip(result,result[1:]))


def test_source_packets_paginate_and_are_not_an_approval(full,full_project):
    c=full;value='日本語の長い要求を全部保持します😀\n'*700
    source=c.k.source(c.owner,full_project[0],value)['id']
    offset=0;packets=[]
    while True:
        page=c.packets.partition(c.owner,source,512,offset=offset,limit=7)
        assert page['text_coverage_complete'] and not page['semantic_requirements_extracted']
        packets+=page['packets']
        if page['next_offset'] is None:break
        offset=page['next_offset']
    restored=[]
    for item in packets:
        restored.append(c.packets.packet(c.owner,source,item['start'],item['end'],item['digest'])['content'])
    assert ''.join(restored)==value
    assert not c.packets.status(c.owner,source)['classified_complete']
    with pytest.raises(Fault):c.packets.packet(c.owner,source,0,5,'0'*64)
    c.k.classify(c.owner,source,0,len(value),'question',[],'Question awaiting semantic requirement extraction')
    state=c.packets.status(c.owner,source)
    assert state['classified_complete'] and state['meaning_review_still_required']


def test_oversized_review_artifact_is_complete_only_with_all_fragment_runs(full,full_project):
    c=full;p=full_project[0];source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    body={'title':'Large design','statement':'設計の段落😀と根拠。\n'*4000}
    large=c.k.propose(c.owner,p,'design',body)
    program=c.p.begin(c.owner,p,source)['program']
    partition=c.scopes.partition(c.owner,program,byte_budget=10000)
    assert len(partition['packets'])>3 and all(x['bytes']<=10000 for x in partition['packets'])
    fragments=[]
    for packet in partition['packets']:
        scope=c.scopes.current(c.owner,packet['id'])
        for item in scope['body']['items']:
            if item['id']==large['id']:fragments.append(item)
        c.rt.review(c.owner,packet['id'],'phase','fixture')
    assert len(fragments)>1
    reassembled=''.join(x['body']['serialized_fragment'] for x in sorted(fragments,key=lambda x:x['fragment']['index']))
    assert json.loads(reassembled)==body
    assert c.scopes.summary(c.owner,program)['complete']
    # Removing just one fragment scope cannot masquerade as whole-artifact coverage.
    c.s.execute("UPDATE review_scopes SET status='superseded' WHERE id=?",(partition['packets'][-1]['id'],))
    summary=c.scopes.summary(c.owner,program)
    assert not summary['complete']
    assert any(x['error']=='missing_review_fragments' for x in summary['failures'])


def test_review_fragments_become_stale_when_source_artifact_changes(full,full_project):
    c=full;p=full_project[0];source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    art=c.k.propose(c.owner,p,'design',{'title':'A','statement':'long specification\n'*4000})
    program=c.p.begin(c.owner,p,source)['program'];parts=c.scopes.partition(c.owner,program,10000)
    c.k.revise(c.owner,art['id'],1,{'title':'A','statement':'new specification'},'Changed design assumption')
    stale=0
    for part in parts['packets']:
        try:c.scopes.current(c.owner,part['id'])
        except Fault as exc:
            assert exc.code=='stale_review_scope';stale+=1
    assert stale>0
