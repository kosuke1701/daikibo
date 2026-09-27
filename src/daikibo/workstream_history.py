"""Cross-record integrity of historical delegation; not fresh review evidence."""
from .common import canonical, digest, need


def validate_history(get, each, project):
    for row in each('workstreams'):
        b = row['body']
        need(row['project']==project and digest(b)==row['digest'] and b['format']=='daikibo.workstream.v1', 'invalid_archive', 'Delegation body differs')
        root = get('breakdowns',row['breakdown'])
        need(root['program']==row['program'] and root['project']==project, 'invalid_archive', 'Delegation belongs to another program')
        get('programs', row['program'])
        by_unit = {u['id']:u for u in root['body']['units']}
        selection = b['selection']; units = selection['units']
        need(units and len(units)==len(set(units)) and set(units)<=set(root['body']['structure']['leaf_units']), 'invalid_archive', 'Invalid delegated leaf units')
        need(selection['tasks']==sorted(t for u in units for t in by_unit[u]['tasks']), 'invalid_archive', 'Delegated task set differs from root')
        need(selection['obligations']==sorted((o for u in units for o in by_unit[u]['obligations']),key=lambda o:(o['requirement'],o['acceptance'])), 'invalid_archive', 'Delegated obligations differ from root')
        need(selection['unit_bindings']=={u:root['body']['material_bindings'][u] for u in units}, 'invalid_archive', 'Delegated unit material differs from root')
        current = row; seen = {row['id']}
        while current['parent']:
            need(current['parent'] not in seen, 'invalid_archive', 'Scope hierarchy has a cycle')
            parent = get('workstreams', current['parent']); seen.add(parent['id'])
            need(parent['project']==project and parent['program']==row['program'] and parent['breakdown']==row['breakdown']
                 and set(current['body']['selection']['units'])<=set(parent['body']['selection']['units']), 'invalid_archive', 'Scope escapes parent')
            current=parent
        if row['previous']:
            previous=get('workstreams',row['previous'])
            need(previous['project']==project and previous['program']==row['program'] and previous['parent']==row['parent'], 'invalid_archive', 'Replacement refers elsewhere')
        packets=list(each('workstream_packets',row['id']))
        need([{'id':p['id'],'digest':p['digest']} for p in packets]==b['packet_manifest'], 'invalid_archive', 'Missing historical scope packet')
        fragments=[];cursor=0
        for ordinal,packet in enumerate(packets):
            p=packet['body']; fragment=p['serialized_fragment']
            need(packet['ordinal']==ordinal and digest(p)==packet['digest'] and p['scope']==row['id']
                 and p['program']==row['program'] and p['start']==cursor and p['end']==cursor+len(fragment)
                 and p['material_digest']==b['material_digest'], 'invalid_archive', 'Scope packet or ordering differs')
            fragments.append(fragment);cursor=p['end']
        need(packets and all(p['body']['total_characters']==cursor for p in packets)
             and digest(''.join(fragments).encode())==b['material_digest'], 'invalid_archive', 'Truncated delegated material')
        from .common import parse_json
        material=parse_json(''.join(fragments),limit=128*1024*1024)
        need(material['selection']==selection and material['title']==b['title'] and material['rationale']==b['rationale']
             and material['parent']==row['parent'] and material['previous']==row['previous']
             and material['breakdown']==row['breakdown'] and material['program']==row['program'], 'invalid_archive', 'Reviewed scope does not match declaration')
        need({u['unit']['id']:digest(u) for u in material['units']}==selection['unit_bindings'],
             'invalid_archive','Retained unit material does not match adopted root')
        need(selection['global']['policy']==root['body']['scope']['policy'],'invalid_archive','Delegated policy differs from root')
        defs={t['id']:t['definition_digest'] for t in root['body']['scope']['tasks']}
        external=[]; owned=set(selection['tasks'])
        for unit in material['units']:
            for task in unit['tasks']:
                for dep in task['dependencies']:
                    if dep not in owned: external.append({'task':task['id'],'dependency':dep,'definition_digest':defs.get(dep)})
        need(sorted(external,key=lambda r:(r['task'],r['dependency']))==selection['external_dependencies'],
             'invalid_archive','External boundary dependencies changed')
        events=list(each('workstream_records',row['id']))
        if row['status'] in {'active','superseded','withdrawn'}:
            need(sum(r['kind']=='adopt' for r in events)==1, 'invalid_archive', 'Active/history scope requires one adoption record')
        if row['status']=='withdrawn':need(any(r['kind']=='withdraw' for r in events), 'invalid_archive', 'Withdrawal record missing')
    for p in each('workstream_packets'):
        get('workstreams',p['scope'])
    for r in each('workstream_records'):
        owner=get('workstreams',r['scope'])
        need(r['project']==owner['project']==project and digest(r['body'])==r['digest'], 'invalid_archive', 'Scope event differs')
        need(r['kind'] in {'adopt','finish','withdraw'}, 'invalid_archive', 'Unknown scope event')
        if r['kind']=='finish':
            need(r['body']['report']['scope']==r['scope'] and r['body']['report']['binding']==r['body']['binding']
                 and r['body']['report']['ready'] is True and r['body']['report']['deploy_ready'] is False,
                 'invalid_archive', 'Scope closure changed or pretends to be release certification')
