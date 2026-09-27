"""Test inventory validation. XML output is a measurement, not semantic proof."""
from __future__ import annotations
import ast
import re
import xml.etree.ElementTree as ET
from .common import Fault, need

def junit(data:bytes,required=()):
    need(len(data)<=32*1024*1024,'report_too_large','Test report exceeds limit')
    upper=data.upper()
    need(b'<!DOCTYPE' not in upper and b'<!ENTITY' not in upper,'unsafe_xml','DTDs and entities are not permitted')
    try:root=ET.fromstring(data)
    except ET.ParseError as exc:raise Fault('invalid_report','Malformed JUnit report') from exc
    need(root.tag in {'testsuites','testsuite'},'invalid_report','Not a JUnit suite')
    cases=[]
    for case in root.iter('testcase'):
        name=case.attrib.get('name','');group=case.attrib.get('classname','')
        need(name,'invalid_report','Unnamed test case')
        full=group+'::'+name if group else name
        status='passed'
        if case.find('failure') is not None or case.find('error') is not None:status='failed'
        elif case.find('skipped') is not None:status='skipped'
        cases.append({'id':full,'name':name,'status':status})
    names={c['id'] for c in cases}|{c['name'] for c in cases}
    missing=sorted(set(required)-names)
    # All skips require explicit separate applicability review; a required check never silently passes skips.
    passed=bool(cases) and all(c['status']=='passed' for c in cases) and not missing
    return {'passed':passed,'count':len(cases),'failed':sum(c['status']=='failed' for c in cases),
            'skipped':sum(c['status']=='skipped' for c in cases),'missing':missing,'cases':cases,
            'meaning':'Inventory observed; independent test-adequacy review remains mandatory.'}

def stub_findings(path:str,data:bytes):
    findings=[]
    content=data.decode('utf-8',errors='replace')
    for n,line in enumerate(content.splitlines(),1):
        if re.search(r'\b(TODO|FIXME|NotImplementedError|IMPLEMENT_ME)\b',line):
            findings.append({'path':path,'line':n,'kind':'placeholder_candidate','text':line[:300]})
    if path.endswith('.py'):
        try:
            tree=ast.parse(content)
        except SyntaxError as exc:
            findings.append({'path':path,'line':exc.lineno,'kind':'syntax_error','text':str(exc)})
            return findings
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                body=[n for n in node.body if not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and isinstance(n.value.value,str))]
                if not body or all(isinstance(n,ast.Pass) or (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and n.value.value is Ellipsis) for n in body):
                    findings.append({'path':path,'line':node.lineno,'kind':'empty_body_candidate','symbol':node.name})
    return findings

def assertion_count(data:bytes):
    try:tree=ast.parse(data.decode())
    except (SyntaxError,UnicodeError):return None
    return sum(isinstance(n,ast.Assert) or isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr.startswith('assert') for n in ast.walk(tree))
