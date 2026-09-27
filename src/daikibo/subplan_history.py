"""Validate stored partial-design history without asserting fresh review evidence."""
from .common import canonical, digest, need, parse_json
from .subplans import FORMAT, MAX_DEPTH, MAX_TREE, pairs, unit_id
from .breakdowns import MAX_PROPOSAL_BYTES, topological


def validate_subplans(get,each,project):
    def tree(ident):
        root=get('subplans',ident);stack=[(root,0)];seen=set();result=[]
        while stack:
            row,depth=stack.pop();b=row['body']
            need(depth<=MAX_DEPTH and len(seen)<MAX_TREE and row['id'] not in seen,'invalid_archive','Duplicate/deep/cyclic child design')
            need(row['project']==project and row['program']==root['program'] and b['program']==row['program'],'invalid_archive','Cross-program child')
            seen.add(row['id']);result.append(row)
            for child in reversed(b['children']):
                item=get('subplans',child['id']);need(item['digest']==child['digest'],'invalid_archive','Child digest differs')
                stack.append((item,depth+1))
        return result

    for row in each('subplans'):
        b=row['body'];ident=row['id'];get('programs',row['program']);tree(ident)
        need(row['project']==project and b['format']==FORMAT and digest(b)==row['digest'],'invalid_archive','Partial plan body differs')
        units={u['id']:u for u in b['units']}
        need(len(units)==len(b['units']) and units,'invalid_archive','Duplicate/empty unit set')
        order=topological(set(units),{k:[u['parent']] if u['parent'] else [] for k,u in units.items()})
        group=unit_id(ident,'__group__')
        need(group in units and units[group]['parent'] is None and sum(u['parent'] is None for u in units.values())==1,
             'invalid_archive','Partial root group missing or duplicated')
        task_ids=[t for u in units.values() for t in u['tasks']]
        obligations=[o for u in units.values() for o in u['obligations']]
        need(len(task_ids)==len(set(task_ids)) and pairs(obligations)==pairs(b['obligations']),
             'invalid_archive','Partial obligations/tasks dropped or duplicated')
        for child in b['children']:
            source=get('subplans',child['id'])
            for original in source['body']['units']:
                copied=units.get(original['id'])
                expected_parent=group if original['parent'] is None else original['parent']
                need(copied and copied['parent']==expected_parent and
                     {k:v for k,v in copied.items() if k not in {'parent','dependencies'}}==
                     {k:v for k,v in original.items() if k not in {'parent','dependencies'}},
                     'invalid_archive','Parent rewrites or omits a child design unit')
        packets=list(each('subplan_packets',ident));fragments=[];cursor=0
        need(packets and [{'id':p['id'],'digest':p['digest']} for p in packets]==b['packet_manifest'],
             'invalid_archive','Missing partial-plan review packet')
        for i,p in enumerate(packets):
            value=p['body'];fragment=value['serialized_fragment']
            need(p['subplan']==ident and p['project']==project and p['ordinal']==i and digest(value)==p['digest']
                 and value['format']=='daikibo.subplan-review.v1' and value['subplan']==ident and value['program']==row['program']
                 and value['material_digest']==b['material_digest'] and value['start']==cursor and value['end']==cursor+len(fragment)
                 and value['required_coverage']==['SPART-'+digest([ident,b['material_digest'],value['start'],value['end']])],
                 'invalid_archive','Partial fragment identity or continuity differs')
            cursor=value['end'];fragments.append(fragment)
        data=''.join(fragments).encode()
        need(all(p['body']['total_characters']==cursor for p in packets) and digest(data)==b['material_digest'],
             'invalid_archive','Incomplete partial design material')
        material=parse_json(data,limit=MAX_PROPOSAL_BYTES)
        need(all(material[k]==b[k] for k in ('format','program','title','rationale','children','units','obligations','boundary_contracts')),
             'invalid_archive','Reviewed partial design differs from proposal')
        arts={a['id']:a for a in material['artifacts']}
        need(len(arts)==len(material['artifacts']),'invalid_archive','Duplicate partial artifact snapshots')
        for a in [*material['artifacts'],*material['global_constraints']]:
            hist=get('revisions',canonical([a['id'],a['revision']]).decode())
            need(digest(a['body'])==a['digest']==hist['digest'] and a['body']==hist['body'],
                 'invalid_archive','Partial input does not match retained canonical revision')
        for source in material['source_index']:
            current=get('sources',source['id'])
            need(all(current[k]==source[k] for k in ('id','project','blob','locator','characters')),'invalid_archive','Original source reference differs')
        tasks={t['id']:t for t in material['tasks']}
        external={t['id']:t for t in material['external_tasks']}
        need(set(tasks)==set(task_ids) and len(tasks)==len(material['tasks']) and len(external)==len(material['external_tasks'])
             and not(set(tasks)&set(external)),'invalid_archive','Task/prerequisite material differs')
        expected_external={d for t in tasks.values() for d in t['dependencies']}-set(tasks)
        need(expected_external==set(external),'invalid_archive','External prerequisite omitted')
        refs=set(b['context_artifacts'])
        for u in units.values():
            if u['domain']:refs.add(u['domain'])
            refs.update(u['interfaces']);refs.update(o['requirement'] for o in u['obligations'])
        for t in [*tasks.values(),*external.values()]:
            refs.update(t['body']['read_artifacts'])
            need(t['dependencies']==sorted(t['body']['dependencies']),'invalid_archive','Task dependency view differs')
            if t['test_plan']:need(digest(t['test_plan']['body'])==t['test_plan']['digest'],'invalid_archive','Frozen checks differ')
            for read in t['reads']:
                hist=get('revisions',canonical([read['artifact'],read['revision']]).decode())
                need(hist['digest']==read['digest'],'invalid_archive','Read-set differs from retained revision')
        need(refs==set(arts),'invalid_archive','Required partial input material is missing or unexplained')
        graph_keys=set()
        for link in material['trace_links']:
            key=(link['source'],link['target'],link['relation'])
            need(key not in graph_keys and (link['source'] in refs or link['target'] in refs)
                 and link['confidence'] in {'asserted','inferred'} and isinstance(link['basis'],str),
                 'invalid_archive','Duplicate/unrelated partial trace input')
            graph_keys.add(key);get('artifacts',link['source']);get('artifacts',link['target'])
        for req,ac in pairs(b['obligations']):
            need(req in arts and arts[req]['kind']=='requirement' and ac in arts[req]['body']['acceptance'],
                 'invalid_archive','Invented requirement/acceptance identity')
        need(b['structure']['task_count']==len(tasks) and b['structure']['obligation_count']==len(pairs(b['obligations'])),
             'invalid_archive','Partial structure summary differs')
    for p in each('subplan_packets'):get('subplans',p['subplan'])
    for row in each('subplan_compositions'):
        b=row['body'];plan=get('subplans',row['subplan']);root=get('breakdowns',row['breakdown'])
        need(row['project']==project and digest(b)==row['digest'] and digest(b['request'])==row['request_digest']
             and b['subplan']==plan['id'] and b['subplan_digest']==plan['digest'] and b['breakdown']==root['id']
             and root['program']==plan['program'] and root['project']==project
             and root['body'].get('origin_subplan')=={'id':plan['id'],'digest':plan['digest']}
             and root['body']['units']==plan['body']['units'] and digest(root['body']['units'])==b['units_digest']
             and root['previous']==b['request']['expected_active'] and b['root_adopted'] is False and b['deploy_ready'] is False
             and b['tasks_created']==[] and b['artifacts_accepted']==[],
             'invalid_archive','Composition differs or claims unperformed adoption')
        expected={(child['id'],p['id'],role) for child in tree(plan['id']) for p in child['body']['packet_manifest'] for role in ('design','trace')}
        observed=[(r['subplan'],r['packet'],r['role']) for r in b['reviews']]
        receipts=[r['receipt'] for r in b['reviews']]
        need(set(observed)==expected and len(observed)==len(expected) and len(set(receipts))==len(receipts)
             and all(isinstance(r,str) and r for r in receipts), 'invalid_archive','Historical partial review references are missing or duplicated')

    composed={r['breakdown']:r for r in each('subplan_compositions')}
    for root in each('breakdowns'):
        origin=root['body'].get('origin_subplan')
        if origin:
            need(root['id'] in composed and composed[root['id']]['subplan']==origin['id'],
                 'invalid_archive','Root lost its partial-composition history')
