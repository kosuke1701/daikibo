"""Concrete findings from detailed contract inspection, not independent review."""
import pytest
from daikibo.common import Fault, canonical, parse_json
from daikibo.contracts import validate_type, violations
from test_reviewed_breakdowns import accepted


@pytest.mark.parametrize('schema',[
    {'type':'object','enum':[{}]},
    {'type':'array','enum':[[]]},
    {'type':'array','items':{'not_a_type':'x'},'enum':[[1]]},
    {'type':'object','properties':{'x':{'oops':True}},'additionalProperties':False,'enum':[{'x':1}]},
])
def test_invalid_nested_enum_schema_is_a_structured_rejection(schema):
    with pytest.raises(Fault):validate_type(schema)


def test_large_integer_contract_is_not_a_float_overflow():
    n=10**500;s={'type':'integer','minimum':n,'maximum':n+10,'enum':[n]}
    validate_type(s);assert not violations(n,s);assert violations(n-1,s)


def test_utf8_json_limit_measures_bytes_not_characters():
    data=canonical({'日本語':'内容'*50})
    assert parse_json(data,limit=len(data))==parse_json(data.decode(),limit=len(data))
    for value in (data,data.decode()):
        with pytest.raises(Fault) as exc:parse_json(value,limit=len(data)-1)
        assert exc.value.code=='too_large'


def test_parent_requirement_and_draft_design_cannot_skip_design_gate(full,full_project):
    c=full;p,r,q,root=full_project
    child=accepted(c,p,'requirement','child condition',acceptance=['CHILD'])
    c.k.link(c.owner,q,child,'decomposes','asserted','Explicit child')
    d=accepted(c,p,'design','Child implementation')
    c.k.link(c.owner,d,child,'realizes','asserted','Implements child only')
    draft=c.k.propose(c.owner,p,'design',{'title':'Unapproved parent approach','statement':'A draft is not an accepted design'})
    c.k.link(c.owner,draft['id'],q,'realizes','asserted','Draft only')
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    prog=c.p.begin(c.owner,p,source)['program']
    c.s.execute("UPDATE programs SET phase='design' WHERE id=?",(prog,))
    row=c.s.one('SELECT * FROM programs WHERE id=?',(prog,))
    assert 'unallocated_requirement:'+q in c.p.phase_blockers(row)
    c.k.accept(c.owner,draft['id'],1)
    assert 'unallocated_requirement:'+q not in c.p.phase_blockers(row)


def test_non_design_link_cannot_discharge_design_allocation(full,full_project):
    c=full;p,r,q,root=full_project
    accepted(c,p,'design','Unlinked design elsewhere')
    scenario=accepted(c,p,'scenario','User scenario is not an implementation design')
    c.k.link(c.owner,scenario,q,'realizes','asserted','Semantically wrong source kind')
    source=c.s.one('SELECT id FROM sources WHERE project=?',(p,))['id']
    prog=c.p.begin(c.owner,p,source)['program']
    row=c.s.one('SELECT * FROM programs WHERE id=?',(prog,));row['phase']='design'
    assert 'unallocated_requirement:'+q in c.p.phase_blockers(row)
