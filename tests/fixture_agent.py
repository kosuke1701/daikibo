"""A deterministic protocol TEST DOUBLE, never a qualified semantic reviewer."""
import json
import sys
from pathlib import Path
p=json.load(sys.stdin)
if p.get('task'):
    goal=p['task']['goal']
    if goal.startswith('WRITE:'):
        spec=json.loads(goal[6:])
        for name,content in spec.items():
            path=Path(name);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
    print(json.dumps({'message':'fixture implementation ran'}))
else:
    context=p.get('context',{})
    acceptance=context.get('task',{}).get('acceptance',[])
    dispositions=[{'id':f['id'],'resolution':'acceptable','reason':'fixture: explicit test-only acceptance, not semantic validation'} for f in context.get('candidate',{}).get('findings',[])]
    scope_packets=[]
    def walk(value):
        if isinstance(value,dict):
            scope=value.get('scope_review')
            if isinstance(scope,dict) and scope.get('format')=='change-scope-review.v1':
                scope_packets.append((value,scope))
            for child in value.values(): walk(child)
        elif isinstance(value,list):
            for child in value: walk(child)
    walk(context)
    seen=set()
    scope_markers=[]
    scope_observations=[]
    for material,scope in scope_packets:
        layer=scope.get('layer','local_repair')
        after_by={item.get('artifact'):item for item in material.get('before_after',[])}
        effects={}
        for item in scope.get('required_dispositions',[]):
            if item.get('kind')!='delta_effect': continue
            artifact=item.get('subject');detail=after_by.get(artifact,{})
            before=detail.get('before',{}).get('body',{});after=detail.get('after',{}).get('body',{})
            changed={key for key in set(before)|set(after) if before.get(key)!=after.get(key)}
            preserved=(not detail.get('after',{}).get('status')=='withdrawn' and
                (not changed or changed<={'title'}))
            controls=after.get('change_control',{})
            if (detail.get('kind')=='interface' and changed<={'statement','change_control','title','description'}
                    and controls.get('verification_ids') and controls.get('consumer_impact')):
                preserved=True
            if preserved:
                resolution='preserves_meaning'
                scope_observations.append({'ref':artifact,'detail':'Fixture compared this exact before/after and source; test-only semantic judgment.'})
            elif detail.get('kind') in {'design','component','test'} and any(
                    path.get('root')==artifact for path in scope.get('upper_contracts',{}).get('upper_paths',[])):
                resolution='within_current_contract'
            else:
                resolution='changes_upper_contract'
            effects[artifact]=resolution
        unresolved=bool(scope.get('unknown_neighbors'))
        has_upper=any(value in {'changes_upper_contract','unknown'} for value in effects.values())
        layer_scope='unresolved' if unresolved else 'upper_scope_required' if has_upper else 'within_scope'
        target=('awaiting_product_decision' if layer_scope=='upper_scope_required' else layer)
        for item in scope.get('required_dispositions',[]):
            marker=item['id'];kind=item.get('kind')
            if marker in seen: continue
            seen.add(marker)
            if kind=='layer_scope': resolution=layer_scope
            elif kind=='layer_target': resolution=target
            elif kind=='delta_effect': resolution=effects[item['subject']]
            elif kind=='review_carry_and_task_fence': resolution='unaffected' if all(
                value=='preserves_meaning' for value in effects.values()) else 'affected'
            elif kind=='interface_consumer': resolution='addressed'
            elif kind=='declared_unknown_consumer': resolution='unresolved'
            else: continue
            scope_markers.append({'id':marker,'resolution':resolution,
                'reason':'Fixture disposition for protocol regression only.'})
    # Large linked-change packets may be externalized. This protocol fixture
    # cannot read tool pages, so it keeps those semantic cases on the explicit
    # upper path and emits the controller-supplied typed IDs.
    for marker in context.get('required_coverage',[]):
        if not isinstance(marker,str) or marker in seen: continue
        if marker.startswith(('scope:','target:','effect:','carry:','consumer:','unknown-neighbor:')):
            seen.add(marker)
            if marker.startswith('scope:'): resolution='upper_scope_required'
            elif marker.startswith('target:'): resolution='awaiting_product_decision'
            elif marker.startswith('effect:'): resolution='changes_upper_contract'
            elif marker.startswith('carry:'): resolution='affected'
            elif marker.startswith('consumer:'): resolution='addressed'
            else: resolution='unresolved'
            scope_markers.append({'id':marker,'resolution':resolution,
                'reason':'External packet fixture uses a conservative test-only upper disposition.'})
    dispositions.extend(scope_markers)
    print(json.dumps({'verdict':'pass','rationale':'Deterministic test double only; not independent AI reasoning.',
                      'covered':context.get('required_coverage',acceptance),'findings':[],
                      'observations':[{'ref':p['subject'],'detail':'Fixture protocol input was received.'}]+scope_observations,
                      'dispositions':dispositions}))
