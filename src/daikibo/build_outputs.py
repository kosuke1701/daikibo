"""D08/D06: exact, collected build outputs between isolated verification steps."""
import os,stat
from pathlib import Path
from .common import digest,inside,need,relative_path

PREFIX='.daikibo-build/'
def validate_definition(value):
    from .common import obj,text
    obj(value,required=('id','repo','path'))
    text(value['id'],'build output ID',200);text(value['repo'],'repository ID',200)
    relative_path(value['path'])
    need(value['path'].startswith(PREFIX) and value['path']!=PREFIX,'invalid_build_output','Build outputs must be under reserved .daikibo-build/')

def location(work,snapshot,item):
    need(item['repo'] in snapshot['repos'],'invalid_build_output','Output repository is not in sealed snapshot')
    return inside(work/snapshot['repos'][item['repo']]['name'],item['path'])

def read_output(path):
    need(path.exists() and not path.is_symlink(),'missing_build_output','Declared output is missing or a symlink')
    st=path.stat();need(stat.S_ISREG(st.st_mode) and st.st_nlink==1,'unsafe_build_output','Output must be a regular file without hardlinks')
    need(st.st_size<=32*1024*1024,'build_output_too_large','Output exceeds current 32 MiB per-file collector bound')
    with path.open('rb') as stream:data=stream.read(32*1024*1024+1)
    need(len(data)<=32*1024*1024,'build_output_too_large','Output grew beyond collector bound')
    return data,0o755 if st.st_mode&0o111 else 0o644

def materialize_inputs(store,work,snapshot,inputs):
    for item in inputs:
        validate_definition({k:item[k] for k in ('id','repo','path')});path=location(work,snapshot,item)
        need(not path.exists(),'build_output_collision','Input output would overwrite an existing file')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o755);data=store.blob_get(item['blob']);path.write_bytes(data)
        # Root-owned immutable-to-worker inputs; replacement attempts are also checked after execution.
        os.chmod(path,0o555 if item.get('mode',0o644)&0o111 else 0o444)

def collect(store,work,snapshot,inputs,definitions):
    for item in inputs:
        data,_=read_output(location(work,snapshot,item))
        need(digest(data)==item['blob'],'build_input_mutated','A verification step changed a previously built input')
    outputs=[]
    for item in definitions:
        validate_definition(item);data,mode=read_output(location(work,snapshot,item))
        outputs.append({**item,'blob':store.blob_put(data),'bytes':len(data),'mode':mode})
    need(sum(o['bytes'] for o in outputs)<=128*1024*1024,'build_outputs_too_large','Output set exceeds current 128 MiB per-check collector bound')
    return outputs
