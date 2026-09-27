"""A real reviewer subprocess can read paginated canonical data from its controller."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

import daikibo
from daikibo.rpc import Server


def test_reviewer_reads_original_source_through_advertised_command(full, full_project, tmp_path, monkeypatch):
    c=full
    source=c.k.source(c.owner,full_project[0],'exact original source beyond a page')
    requirement=c.k.propose(c.owner,full_project[0],'requirement',{
        'title':'Review access', 'statement':'Read the original source',
        'acceptance':['Read exact source'], 'source_refs':[source['id']]})
    script=tmp_path/'reader.py'
    script.write_text('''import json,subprocess,sys
p=json.load(sys.stdin); access=p['context']['read_access']
source=p['context']['artifact']['body']['source_refs'][0]
parts=[];start=0
while True:
    params={'source':source,'start':start,'limit':7}
    result=json.loads(subprocess.check_output(access['command']+['call','source.read','--json',json.dumps(params)],cwd='/tmp'))
    parts.append(result['content'])
    if result['next_start'] is None:break
    start=result['next_start']
print(json.dumps({'verdict':'pass','rationale':'Protocol fixture, not semantic acceptance','covered':['Read exact source'],'findings':[],'observations':[{'ref':source,'detail':''.join(parts)}],'dispositions':[]}))
''')
    monkeypatch.setenv('PYTHONPATH',str(Path(daikibo.__file__).resolve().parent.parent))
    c.rt.adapters.register(c.owner,'read-fixture','fixture',sys.executable,[str(script)])
    with tempfile.TemporaryDirectory(prefix='review-rpc-') as folder:
        server=Server(c,Path(folder)/'socket')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            reviewed=c.rt.review(c.owner,requirement['id'],'requirements','read-fixture')
            assert reviewed['result']['observations']==[{'ref':source['id'],'detail':'exact original source beyond a page'}]
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close()
