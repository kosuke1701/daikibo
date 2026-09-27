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
    print(json.dumps({'verdict':'pass','rationale':'Deterministic test double only; not independent AI reasoning.',
                      'covered':acceptance,'findings':[], 'observations':[{'ref':p['subject'],'detail':'Fixture protocol input was received.'}], 'dispositions':dispositions}))
