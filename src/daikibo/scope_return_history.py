"""Portable return-review tree validation, not fresh operational evidence."""
from .common import canonical, digest, need
from .scope_returns import MAX_BYTES, MAX_LEVELS, MAX_PACKETS, packet_check


def judgment_check(ref, packet):
    need(isinstance(ref,dict) and ref.get('packet')==packet['id'] and ref.get('packet_digest')==packet['digest']
         and isinstance(ref.get('receipt'),str) and bool(ref['receipt'])
         and isinstance(ref.get('run'),str) and bool(ref['run']),
         'invalid_archive','Historical child judgment refers elsewhere')
    result=ref.get('result')
    need(isinstance(result,dict) and result.get('verdict')=='pass' and result.get('findings')==[]
         and isinstance(result.get('rationale'),str) and result['rationale'].strip()
         and isinstance(result.get('covered'),list)
         and set(packet['body']['required_coverage'])<=set(result['covered']),
         'invalid_archive','Historical synthesis requires complete passed child judgments')


def validate_returns(get,each,project):
    for row in each('scope_returns'):
        b=row['body'];scope=get('workstreams',row['scope']);material=b['material']
        need(row['project']==scope['project']==project and digest(b)==row['digest']
             and b['format']=='daikibo.scope-return.v1' and row['status'] in {'proposed','applied','abandoned'}
             and type(b['byte_budget']) is int and 4096<=b['byte_budget']<=100000
             and digest(material)==b['material_digest'] and len(canonical(b))<=MAX_BYTES,
             'invalid_archive','Historical return proposal identity differs')
        need(material['format']=='daikibo.scope-return-material.v1' and material['scope']==scope['id']
             and material['scope_digest']==scope['digest'] and material['retained_selection']==scope['body']['selection']
             and material['program']==scope['program'] and material['children']==[]
             and isinstance(material['reason'],str) and material['reason'].strip(),
             'invalid_archive','Historical return drops or substitutes scope responsibilities')
        if material['current_root']:
            root=get('breakdowns',material['current_root']['id'])
            need(root['project']==project and root['program']==scope['program'] and root['digest']==material['current_root']['digest'],
                 'invalid_archive','Return current root differs')
        if material['parent']:
            parent=get('workstreams',material['parent']['id'])
            need(parent['id']==scope['parent'] and parent['digest']==material['parent']['digest'],
                 'invalid_archive','Return parent differs')
        else:need(scope['parent'] is None,'invalid_archive','Return parent is missing')
        selected=material['retained_selection']
        tasks=sorted(set(selected['tasks'])|{d['dependency'] for d in selected['external_dependencies']})
        need([t['id'] for t in material['tasks']]==tasks and all(t['project']==project for t in material['tasks']),
             'invalid_archive','Return task/prerequisite inventory differs')
        needed=sorted({a for t in material['tasks'] for a in t['read_artifacts']})
        need([a['id'] for a in material['artifacts']]==needed,'invalid_archive','Current input inventory is incomplete')
        for a in material['artifacts']:
            hist=get('revisions',canonical([a['id'],a['revision']]).decode())
            need(digest(a['body'])==a['digest']==hist['digest'] and a['body']==hist['body'],
                 'invalid_archive','Return artifact is not the recorded original revision')
        packets=list(each('scope_return_packets',row['id']));by_id={p['id']:p for p in packets}
        need(len(packets)==len(by_id),'invalid_archive','Duplicate return review nodes')
        source_nodes=sorted((p for p in packets if p['level']==0),key=lambda p:p['ordinal'])
        need(source_nodes and len(source_nodes)<=MAX_PACKETS
             and [{'id':p['id'],'digest':p['digest']} for p in source_nodes]==b['leaf_manifest'],
             'invalid_archive','Return source manifest differs')
        cursor=0;fragments=[]
        for i,p in enumerate(source_nodes):
            packet_check(p,row);value=p['body'];fragment=value['serialized_fragment']
            need(p['ordinal']==i and value['start']==cursor and value['end']==cursor+len(fragment),
                 'invalid_archive','Return fragment continuity differs')
            cursor=value['end'];fragments.append(fragment)
        need(all(p['body']['total_characters']==cursor for p in source_nodes)
             and ''.join(fragments).encode()==canonical(material), 'invalid_archive','Return source material is incomplete')
        for p in packets:
            packet_check(p,row)
            if p['level']>0:
                refs=p['body']['children']
                need(len({r['packet'] for r in refs})==len(refs),'invalid_archive','Repeated synthesis child')
                for ref in refs:
                    need(ref['packet'] in by_id,'invalid_archive','Missing synthesis child')
                    child=by_id[ref['packet']]
                    need(child['level']==p['level']-1,'invalid_archive','Return synthesis must progress by one level')
                    judgment_check(ref,child)
        if row['status']=='applied':
            result=row['result'];need(isinstance(result,dict),'invalid_archive','Applied return has no result')
            event=get('workstream_records',result['record']);body=event['body']
            need(event['kind']=='withdraw' and event['scope']==scope['id'] and event['project']==project
                 and body['proposal']==row['id'] and body['proposal_digest']==row['digest']
                 and body['material_digest']==b['material_digest'] and body['reason']==material['reason']
                 and body['tasks_cancelled']==[] and body['root_scope_reduced'] is False and body['deploy_ready'] is False
                 and all(result.get(k)==v for k,v in body.items()) and result['scope']==scope['id']
                 and result['status']=='withdrawn' and scope['status']=='withdrawn',
                 'invalid_archive','Applied return and history are inconsistent or imply scope reduction')
            root=by_id.get(result['root_packet'])
            need(root and root['level']>0 and root['digest']==body['root_digest'], 'invalid_archive','Missing final synthesized root')
            judgment_check(body['root_judgment'],root)
            need(body['root_judgment']['receipt']==body['review_receipt'], 'invalid_archive','Root review reference differs')
            stack=[root];seen=set();covered=[]
            while stack:
                node=stack.pop();need(node['id'] not in seen,'invalid_archive','Overlapping return synthesis tree');seen.add(node['id'])
                if node['level']==0:covered.append(node['id'])
                else:stack.extend(by_id[ref['packet']] for ref in reversed(node['body']['children']))
            need(covered==[p['id'] for p in source_nodes], 'invalid_archive','Final return omits or reorders source obligations')
        elif row['status']=='proposed':need(row['result'] is None,'invalid_archive','Open proposal has an applied result')
        else:need(isinstance(row['result'],dict) and row['result'].get('scope_unchanged') is True,
                  'invalid_archive','Abandoned proposal changes responsibility')
    for p in each('scope_return_packets'):
        owner=get('scope_returns',p['proposal']);packet_check(p,owner)
    for event in each('workstream_records'):
        if event['kind']=='withdraw' and 'proposal' in event['body']:
            proposal=get('scope_returns',event['body']['proposal'])
            need(proposal['status']=='applied' and proposal['result']['record']==event['id'],
                 'invalid_archive','Return record has no mutually linked applied proposal')
