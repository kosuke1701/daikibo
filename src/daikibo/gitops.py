"""Content-addressed multi-repository snapshots; Git never decides completion."""
from __future__ import annotations
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from .common import Fault, canonical, digest, inside, need, relative_path, text, timestamp, uid

EXCLUDED_DIRS={'.daikibo-build','.git','.venv','venv','node_modules','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','dist','build'}
EXCLUDED_FILES={'.git','.env','.env.local','id_rsa','id_ed25519','credentials.json'}

def git(path: Path, *args: str, input: bytes | None = None, check=True, timeout=60, env_extra=None):
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(path),'LANG':'C.UTF-8',
         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0',
         'GIT_AUTHOR_NAME':'daikibo_dev','GIT_AUTHOR_EMAIL':'daikibo@localhost',
         'GIT_COMMITTER_NAME':'daikibo_dev','GIT_COMMITTER_EMAIL':'daikibo@localhost'}
    if env_extra: env.update(env_extra)
    result=subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','protocol.file.allow=never','-C',str(path),*args],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=timeout)
    if check and result.returncode:
        raise Fault('git_failed','Git operation failed',{'argv':list(args),'stderr':result.stderr.decode(errors='replace')[-4000:]})
    return result

class Snapshots:
    def __init__(self,store,security,knowledge): self.s,self.sec,self.k=store,security,knowledge

    @staticmethod
    def _ignored_by_repo(snapshot, ignored):
        """Map exact declared report paths to the repository they can affect.

        A single-repository worker's cwd is that repository root, while a
        multi-repository worker's cwd is the work root and therefore requires
        the repository name as the first path component.  Unqualified paths
        in a multi-repository worker are outside every repository scan and are
        deliberately not expanded into every repository.
        """
        repos=list(snapshot.get('repos', {}).items())
        result={rid: [] for rid, _repo in repos}
        if not ignored:
            return result
        paths=[]
        for path in ignored:
            path=relative_path(path)
            if path not in paths:
                paths.append(path)
        if len(repos)==1:
            result[repos[0][0]]=paths
            return result
        by_name={repo['name']: rid for rid,repo in repos}
        for path in paths:
            parts=PurePosixPath(path).parts
            if len(parts) <= 1:
                continue
            rid=by_name.get(parts[0])
            if rid is not None:
                result[rid].append(str(PurePosixPath(*parts[1:])))
        return result

    def register(self,actor,project,name,path):
        actor.require('owner',project=project);self.k.project(actor,project)
        text(name,'repository name',100)
        need(name==relative_path(name) and '/' not in name and name not in {'.','..'},'invalid_name','Use a single path component as repository name')
        root=Path(path).resolve()
        need(root.is_dir() and not root.is_relative_to(self.s.home) and not self.s.home.is_relative_to(root),'unsafe_repository','Repository and protected control home must not overlap')
        head=git(root,'rev-parse','HEAD',check=False)
        oid=head.stdout.decode().strip() if head.returncode==0 else None
        ident=uid('REPO')
        with self.s.transaction():
            self.s.execute("INSERT INTO repos VALUES(?,?,?,?,?)",(ident,project,name,str(root),oid))
            self.sec.event(project,'repository_registered',actor.id,{'id':ident,'name':name,'head':oid})
        return {'id':ident,'name':name,'head':oid}

    def scan(self,root:Path,store_blobs=True,ignored=()):
        files={}; unknown=[]; total=0
        for current,dirs,names in os.walk(root,followlinks=False):
            for directory in dirs:
                link=Path(current,directory)
                if link.is_symlink() and directory not in EXCLUDED_DIRS:
                    need(link.resolve().is_relative_to(root.resolve()),'unsafe_symlink','Source directory symlink escapes repository',str(link))
                    files[link.relative_to(root).as_posix()]={'kind':'symlink','target':os.readlink(link),'mode':0o120000}
            dirs[:]=sorted(d for d in dirs if d not in EXCLUDED_DIRS and not Path(current,d).is_symlink())
            for name in sorted(names):
                path=Path(current,name);rel=path.relative_to(root).as_posix()
                if name in EXCLUDED_FILES or rel in ignored: continue
                st=path.lstat()
                if stat.S_ISLNK(st.st_mode):
                    target=os.readlink(path)
                    need(not os.path.isabs(target) and path.resolve().is_relative_to(root.resolve()),'unsafe_symlink','Source symlink escapes repository',rel)
                    files[rel]={'kind':'symlink','target':target,'mode':0o120000}
                    continue
                need(stat.S_ISREG(st.st_mode),'unsafe_file','Source contains a device, FIFO or socket',rel)
                need(st.st_size<=32*1024*1024,'source_too_large','Individual source file exceeds 32 MiB; classify large assets separately',rel)
                data=path.read_bytes();total+=len(data)
                need(total<=2*1024*1024*1024,'snapshot_too_large','Source snapshot exceeds 2 GiB')
                h=self.s.blob_put(data) if store_blobs else digest(data)
                files[rel]={'kind':'file','blob':h,'mode':0o100755 if st.st_mode&0o111 else 0o100644,'size':len(data)}
        return {'files':files,'bytes':total,'unknown':unknown}

    def capture(self,actor,project,repos=None,store_blobs=True):
        self.k.project(actor,project)
        rows=self.s.all("SELECT * FROM repos WHERE project=? ORDER BY name",(project,))
        if repos is not None:
            rows=[r for r in rows if r['id'] in repos]
            need(len(rows)==len(repos),'missing_repository','Requested repository is not registered')
        need(rows,'no_repository','Register at least one repository')
        body={'format':'snapshot.v1','repos':{}}
        for r in rows:
            head=git(Path(r['path']),'rev-parse','HEAD',check=False)
            content=self.scan(Path(r['path']),store_blobs=store_blobs)
            body['repos'][r['id']]={'name':r['name'],'head':head.stdout.decode().strip() if head.returncode==0 else None,**content}
        body['digest']=digest(body)
        return body

    def materialize(self,snapshot,destination:Path,readonly=False,owner_uid=None):
        destination.mkdir(parents=True,exist_ok=True,mode=0o755)
        for rid,repo in snapshot['repos'].items():
            root=destination/relative_path(repo['name']);root.mkdir(parents=True,exist_ok=True,mode=0o755)
            # Files first, symlinks last, and no writes through symlink parents.
            items=sorted(repo['files'].items(),key=lambda x:x[1]['kind']=='symlink')
            for rel,entry in items:
                path=inside(root,rel)
                path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                need(not path.exists() and not path.is_symlink(),'snapshot_collision','Snapshot contains conflicting paths')
                if entry['kind']=='symlink':
                    path.symlink_to(entry['target'])
                    need(path.resolve().is_relative_to(root.resolve()),'unsafe_symlink','Snapshot symlink escapes workspace')
                else:
                    path.write_bytes(self.s.blob_get(entry['blob']))
                    os.chmod(path,0o755 if entry['mode']==0o100755 else 0o644)
            for current,dirs,files in os.walk(root,followlinks=False):
                for name in files:
                    path=Path(current,name)
                os.chmod(current,0o755)
        return destination

    def collect(self,original,directory:Path,ignored=()):
        body={'format':'snapshot.v1','repos':{}}
        ignored_by_repo=self._ignored_by_repo(original,ignored)
        for rid,repo in original['repos'].items():
            root=directory/repo['name']
            need(root.is_dir() and not root.is_symlink(),'missing_repository','Worker removed or replaced repository root')
            content=self.scan(root,ignored=ignored_by_repo.get(rid,()))
            body['repos'][rid]={'name':repo['name'],'head':repo.get('head'),**content}
        body['digest']=digest(body)
        return body

    @staticmethod
    def changes(before,after,ignored=()):
        result=[]
        ignored_by_repo=Snapshots._ignored_by_repo(before,ignored)
        for rid in before['repos'].keys() | after['repos'].keys():
            a=before['repos'].get(rid,{});b=after['repos'].get(rid,{})
            excluded=set(ignored_by_repo.get(rid,()))
            for path in a.get('files',{}).keys() | b.get('files',{}).keys():
                if path in excluded:
                    continue
                if a.get('files',{}).get(path)!=b.get('files',{}).get(path):
                    result.append({'repo':rid,'repo_name':b.get('name',a.get('name')),'path':path,
                                   'before':a.get('files',{}).get(path),'after':b.get('files',{}).get(path)})
        return sorted(result,key=lambda x:(x['repo'],x['path']))

    def commit_snapshot(self,snapshot,rid,message,ref='refs/heads/daikibo',expected=None,reconcile_only=False):
        repo=snapshot['repos'][rid]
        bare=self.s.home/'git'/rid
        if not bare.exists():
            bare.mkdir(parents=True,mode=0o700)
            git(bare,'init','--bare')
        objects={}
        for path,entry in repo['files'].items():
            content=entry['target'].encode() if entry['kind']=='symlink' else self.s.blob_get(entry['blob'])
            oid=git(bare,'hash-object','-w','--stdin',input=content).stdout.decode().strip()
            objects[path]=(entry['mode'],oid)
        def tree(prefix=''):
            entries=[];directories=set()
            for path,(mode,oid) in objects.items():
                if not path.startswith(prefix): continue
                rest=path[len(prefix):]
                if '/' in rest: directories.add(rest.split('/',1)[0])
                else: entries.append(f'{mode:o} blob {oid}\t'.encode()+rest.encode()+b'\0')
            for name in sorted(directories):
                oid=tree(prefix+name+'/')
                entries.append(f'040000 tree {oid}\t'.encode()+name.encode()+b'\0')
            return git(bare,'mktree','-z',input=b''.join(entries)).stdout.decode().strip()
        tree_oid=tree()
        # Retain the actual repository history as the parent of the first managed commit.
        imported_parent=None
        if repo.get('head'):
            need(__import__('re').fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',repo['head']),'invalid_git_head','Expected a Git object ID')
            present=git(bare,'cat-file','-e',repo['head']+'^{commit}',check=False)
            if present.returncode:
                registered=self.s.one('SELECT path FROM repos WHERE id=?',(rid,),True)
                archive=bare/'original.bundle'
                git(Path(registered['path']),'bundle','create',str(archive),'--all','HEAD')
                git(bare,'bundle','unbundle',str(archive))
                git(bare,'cat-file','-e',repo['head']+'^{commit}')
                archive.unlink(missing_ok=True)
            imported_parent=repo['head']
        current=git(bare,'rev-parse','--verify',ref,check=False)
        current_oid=current.stdout.decode().strip() if current.returncode==0 else None
        if expected is not None: need(current_oid==expected,'git_conflict','Protected branch changed')
        if current_oid:
            current_tree=git(bare,'rev-parse',current_oid+'^{tree}').stdout.decode().strip()
            if current_tree==tree_oid:
                return {'repository':rid,'git_dir':str(bare),'commit':current_oid,'tree':tree_oid,'ref':ref,'snapshot':snapshot['digest'],'reconciled':True}
        need(not reconcile_only,'git_conflict','Existing branch does not match the sealed tree; no branch was changed')
        args=['commit-tree',tree_oid]
        if current_oid or imported_parent:args+=['-p',current_oid or imported_parent]
        commit=git(bare,*args,input=(message+'\n').encode()).stdout.decode().strip()
        git(bare,'update-ref',ref,commit,current_oid or '0'*40)
        return {'repository':rid,'git_dir':str(bare),'commit':commit,'tree':tree_oid,'ref':ref,'snapshot':snapshot['digest']}
