"""Small stdlib-only PEP 517 wheel backend. No downloaded build tooling required."""
from __future__ import annotations
import base64,csv,hashlib,io,os,tarfile,tomllib,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def configuration():return tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
def metadata():
    config=configuration();name=config['name'].replace('-','_');version=config['version'];dist=f'{name}-{version}.dist-info'
    lines=['Metadata-Version: 2.4',f'Name: {config["name"]}',f'Version: {version}',f'Summary: {config["description"]}',f'Requires-Python: {config["requires-python"]}','License-Expression: MIT','License-File: LICENSE','Description-Content-Type: text/markdown']
    lines += ['Requires-Dist: '+d for d in config['dependencies']]
    for extra,deps in config.get('optional-dependencies',{}).items():
        lines.append('Provides-Extra: '+extra);lines+=['Requires-Dist: '+d+f'; extra == "{extra}"' for d in deps]
    text='\n'.join(lines)+'\n\n'+(ROOT/'README.md').read_text()
    return name,version,dist,text.encode()
def get_requires_for_build_wheel(config_settings=None):return []
def prepare_metadata_for_build_wheel(metadata_directory,config_settings=None):
    name,version,dist,meta=metadata();directory=Path(metadata_directory)/dist;directory.mkdir(parents=True,exist_ok=True);(directory/'METADATA').write_bytes(meta);return dist

def build_wheel(wheel_directory,config_settings=None,metadata_directory=None):
    name,version,dist,meta=metadata();directory=Path(wheel_directory);directory.mkdir(parents=True,exist_ok=True);filename=f'{name}-{version}-py3-none-any.whl';files={}
    for p in sorted((ROOT/'src'/'daikibo').rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}:files[p.relative_to(ROOT/'src').as_posix()]=p.read_bytes()
    files[dist+'/METADATA']=meta
    files[dist+'/WHEEL']=b'Wheel-Version: 1.0\nGenerator: daikibo-stdlib-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    files[dist+'/entry_points.txt']=b'[console_scripts]\ndaikibo = daikibo.cli:main\n'
    files[dist+'/licenses/LICENSE']=(ROOT/'LICENSE').read_bytes()
    record=io.StringIO();writer=csv.writer(record,lineterminator='\n')
    for path,data in sorted(files.items()):writer.writerow([path,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),str(len(data))])
    writer.writerow([dist+'/RECORD','','']);files[dist+'/RECORD']=record.getvalue().encode()
    with zipfile.ZipFile(directory/filename,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for path,data in sorted(files.items()):
            info=zipfile.ZipInfo(path,(2026,9,11,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;z.writestr(info,data)
    return filename

def build_sdist(sdist_directory,config_settings=None):
    name,version,_,_=metadata();filename=f'{name}-{version}.tar.gz';dest=Path(sdist_directory);dest.mkdir(parents=True,exist_ok=True)
    with tarfile.open(dest/filename,'w:gz') as archive:
        for p in sorted(ROOT.rglob('*')):
            rel=p.relative_to(ROOT)
            if any(x in {'.git','.venv','dist','offline_dependencies','__pycache__','.pytest_cache'} for x in rel.parts):continue
            if p.is_file():archive.add(p,arcname=f'{name}-{version}/'+rel.as_posix(),recursive=False)
    return filename
if __name__=='__main__':print(build_wheel(str(ROOT/'dist')))
