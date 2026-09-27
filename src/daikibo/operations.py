"""Install/runtime diagnostics, consistent backups, restart recovery and retention."""
from __future__ import annotations
import io
import hashlib
import os
import platform
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from .common import Actor,Fault,atomic_write,canonical,digest,need,parse_json,timestamp,uid
from .db import SCHEMA_VERSION
from .failure_retention import (PENDING_INVENTORY_FORMAT, PENDING_INVENTORY_SUFFIX,
                                 PENDING_INVENTORY_MAX_BYTES)
from .backup_artifacts import (DirectoryArtifactStore, direct_physical_mapping,
                               freeze_file_to_leaves, publish_recipe,
                               recipe_from_stored_zip, validate_all_recipes)
from .program_origins import validate_origin_database, validate_origin_store


def referenced_blob_hashes(path,chunk_bytes=1024*1024):
    """Conservative bounded-memory scan, including hashes split across chunks.

    Large snapshots must not lose their transitive file references during GC.
    False-positive hashes retain extra data rather than deleting live evidence.
    """
    tail=b''
    with path.open('rb') as stream:
        while chunk:=stream.read(chunk_bytes):
            window=tail+chunk
            for found in re.findall(rb'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',window):yield found.decode()
            tail=window[-128:]

MAX_BACKUP_FILE_BYTES = 64 * 1024**3
MAX_BACKUP_TOTAL_BYTES = 256 * 1024**3
MAX_BACKUP_MANIFEST_BYTES = 64 * 1024**2


def copy_hashed(source, destination, limit=MAX_BACKUP_FILE_BYTES):
    """Bounded-memory transfer; content identity is computed on the bytes copied."""
    h=hashlib.sha256();total=0
    while block:=source.read(1024*1024):
        total+=len(block)
        need(total<=limit,'backup_capacity','File exceeds the explicit backup limit')
        h.update(block);destination.write(block)
    return {'sha256':h.hexdigest(),'bytes':total}


def file_hash(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _safe_home_relative(home, value, *, field, require_existing=False):
    """Return a structurally checked path relative to the control home.

    Recovery paths are persisted as relative members in the backup inventory.
    The check deliberately uses the control-home boundary rather than a
    component name such as ``recovery``; a parent directory may legitimately
    contain that name.
    """
    home = Path(home).absolute()
    path = Path(value)
    if not path.is_absolute():
        path = home / path
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise Fault("pending_backup_scope", "Pending recovery path is outside the control home", field)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise Fault("pending_backup_scope", "Pending recovery path is not a safe relative member", field)
    try:
        resolved = path.resolve(strict=False)
        home_resolved = home.resolve(strict=True)
    except OSError as exc:
        raise Fault("pending_backup_incomplete", "Pending recovery path cannot be resolved", {"field": field, "error": str(exc)})
    if not resolved.is_relative_to(home_resolved):
        raise Fault("pending_backup_scope", "Pending recovery path escapes the control home", field)
    if require_existing and (not path.exists() or path.is_symlink()):
        raise Fault("pending_backup_incomplete", "Pending recovery root is missing or is a symlink", field)
    return relative.as_posix()


def _special_kind(st):
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISFIFO(st.st_mode):
        return "fifo"
    if stat.S_ISSOCK(st.st_mode):
        return "socket"
    if stat.S_ISCHR(st.st_mode):
        return "character_device"
    if stat.S_ISBLK(st.st_mode):
        return "block_device"
    return "other"


def _inventory_identity(item):
    """Return the fields whose disagreement is a backup conflict."""
    identity = {key: item.get(key) for key in ("path", "root", "kind", "mode", "bytes")}
    if item.get("kind") == "file":
        identity["sha256"] = item.get("sha256")
    if item.get("kind") == "symlink":
        identity["target"] = item.get("target")
    return identity


def _validate_inventory_shape(inventory, run):
    need(isinstance(inventory, dict) and inventory.get("format") == PENDING_INVENTORY_FORMAT,
         "pending_backup_incomplete", "Pending recovery inventory format differs", run)
    need(inventory.get("run") == run, "pending_backup_incomplete",
         "Pending recovery inventory run differs", run)
    roots = inventory.get("roots")
    need(isinstance(roots, dict), "pending_backup_incomplete",
         "Pending recovery inventory roots are malformed", run)
    for key, relative in roots.items():
        need(key in {"worker_root", "staged_root", "work_root"}, "pending_backup_incomplete",
             "Pending recovery inventory has an unknown root", key)
        need(isinstance(relative, str) and relative, "pending_backup_incomplete",
             "Pending recovery inventory root is not a string", key)
        member = PurePosixPath(relative)
        need(not member.is_absolute() and "\\" not in relative and ".." not in member.parts,
             "pending_backup_incomplete", "Pending recovery inventory root is unsafe", key)
    raw_root = inventory.get("raw_root")
    need(raw_root in {"worker_root", "staged_root"} and raw_root in roots and "work_root" in roots,
         "pending_backup_incomplete", "Pending recovery inventory has no raw/work root", run)
    need(PurePosixPath(roots["work_root"]) == PurePosixPath(roots[raw_root]) / "work",
         "pending_backup_incomplete", "Pending recovery work root is not structurally bound", run)
    entries = inventory.get("entries")
    need(isinstance(entries, list), "pending_backup_incomplete",
         "Pending recovery inventory entries are malformed", run)
    seen = set()
    for item in entries:
        need(isinstance(item, dict), "pending_backup_incomplete",
             "Pending recovery inventory entry is malformed", run)
        path = item.get("path");root = item.get("root");kind = item.get("kind")
        need(isinstance(path, str) and path and not PurePosixPath(path).is_absolute()
             and "\\" not in path and ".." not in PurePosixPath(path).parts,
             "pending_backup_incomplete", "Pending recovery inventory entry path is unsafe", path)
        need(root in {"worker_root", "staged_root"} and root in roots,
             "pending_backup_incomplete", "Pending recovery inventory entry root is unknown", path)
        need(PurePosixPath(path).is_relative_to(PurePosixPath(roots[root])),
             "pending_backup_incomplete", "Pending recovery inventory entry escapes its root", path)
        need(kind in {"file", "symlink", "fifo", "socket", "character_device", "block_device", "other"},
             "pending_backup_incomplete", "Pending recovery inventory entry kind is unsupported", path)
        need(path not in seen, "pending_backup_conflict",
             "Pending recovery inventory contains duplicate paths", path)
        seen.add(path)
        need(type(item.get("mode")) is int and type(item.get("bytes")) is int and item["bytes"] >= 0,
             "pending_backup_incomplete", "Pending recovery inventory metadata is incomplete", path)
        if kind == "file":
            value = item.get("sha256")
            need(isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value),
                 "pending_backup_incomplete", "Pending regular inventory entry has no valid hash", path)
        elif kind == "symlink":
            need(isinstance(item.get("target"), str), "pending_backup_incomplete",
                 "Pending symlink metadata has no target", path)
    provenance = inventory.get("provenance")
    if provenance is not None:
        need(isinstance(provenance, dict) and type(provenance.get("generation")) is int
             and provenance["generation"] >= 1, "pending_backup_incomplete",
             "Pending recovery inventory provenance is malformed", run)
        previous = provenance.get("previous_digest")
        need(previous is None or (isinstance(previous, str) and len(previous) == 64
                                  and all(char in "0123456789abcdef" for char in previous)),
             "pending_backup_incomplete", "Pending recovery inventory provenance digest is malformed", run)


def _pending_recovery_inventory(home, marker):
    """Return exact pending raw files and metadata, failing closed on gaps.

    The ordinary system backup is the recovery boundary for pending collector
    bytes.  A scan error, missing root, or unsafe root therefore aborts the
    backup instead of producing an archive that can falsely look complete.
    Special filesystem entries are represented as metadata in a sidecar
    inventory; they are never followed or executed by restore/reconcile.
    """
    home = Path(home).absolute()
    state = marker.get("state")
    summary = marker.get("summary") or {}
    needs_raw = state in {"running", "pending", "pending_staging", "staging", "marker_error"} or summary.get("status") != "complete"
    roots = {}
    root_paths = {}
    for key in ("worker_root", "staged_root"):
        value = marker.get(key)
        if not value:
            continue
        relative = _safe_home_relative(home, value, field=key, require_existing=needs_raw)
        path = home / PurePosixPath(relative)
        if not path.exists():
            if needs_raw:
                raise Fault("pending_backup_incomplete", "Pending recovery root is missing", key)
            continue
        if path.is_symlink() or not path.is_dir():
            raise Fault("pending_backup_incomplete", "Pending recovery root is not a real directory", key)
        roots[key] = relative
        root_paths[key] = path
    if needs_raw and not root_paths:
        raise Fault("pending_backup_incomplete", "Pending recovery has no readable raw root", marker.get("run"))
    if not root_paths:
        sidecar = home / "recovery" / "pending" / f"{marker.get('run')}{PENDING_INVENTORY_SUFFIX}"
        need(not sidecar.exists() and not marker.get("inventory_required"), "pending_backup_incomplete",
             "Pending recovery inventory exists without its raw root", marker.get("run"))
        return None, []

    # A staged root is the canonical raw tree after cleanup staging.  During a
    # narrow transition both keys can exist; retain both only if they map to
    # distinct paths and reject any ambiguous duplicate member.
    raw_files = []
    actual_entries = []
    seen = set()
    for root_key, root in root_paths.items():
        stack = [root]
        while stack:
            current = stack.pop()
            # PermissionError, ENOENT, and all other scan failures intentionally
            # propagate.  This is the exactness boundary for system.backup.
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    st = entry.stat(follow_symlinks=False)
                    relative = path.relative_to(home).as_posix()
                    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                        stack.append(path)
                        continue
                    if relative in seen:
                        raise Fault("pending_backup_duplicate", "Pending recovery inventory contains a duplicate member", relative)
                    seen.add(relative)
                    if stat.S_ISREG(st.st_mode):
                        before = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)
                        checksum = file_hash(path)
                        after = path.stat()
                        need(before == (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns),
                             "pending_backup_conflict", "Pending regular file changed while inventory was collected", relative)
                        raw_files.append((relative, path))
                        actual_entries.append({"path": relative, "kind": "file",
                                               "mode": stat.S_IFMT(st.st_mode) | stat.S_IMODE(st.st_mode),
                                               "bytes": int(st.st_size), "sha256": checksum,
                                               "root": root_key})
                        continue
                    item = {"path": relative, "kind": _special_kind(st),
                            "mode": stat.S_IFMT(st.st_mode) | stat.S_IMODE(st.st_mode),
                            "bytes": int(st.st_size), "mtime_ns": int(st.st_mtime_ns),
                            "root": root_key}
                    if stat.S_ISLNK(st.st_mode):
                        item["target"] = os.readlink(path)
                    actual_entries.append(item)
    raw_root = "staged_root" if "staged_root" in roots else "worker_root"
    roots["work_root"] = f"{roots[raw_root]}/work"
    run = marker.get("run")
    current = {
        "format": PENDING_INVENTORY_FORMAT, "run": run, "roots": roots,
        "raw_root": raw_root, "entries": sorted(actual_entries, key=lambda item: item["path"]),
    }
    sidecar = home / "recovery" / "pending" / f"{run}{PENDING_INVENTORY_SUFFIX}"
    existing = None
    if sidecar.exists():
        need(sidecar.is_file() and not sidecar.is_symlink(), "pending_backup_incomplete",
             "Pending recovery inventory sidecar is not a regular file", run)
        existing = parse_json(sidecar.read_bytes(), limit=PENDING_INVENTORY_MAX_BYTES)
        _validate_inventory_shape(existing, run)
        need(existing["roots"] == roots and existing["raw_root"] == raw_root,
             "pending_backup_conflict", "Pending recovery inventory root binding differs", run)
        expected = marker.get("inventory_digest")
        if expected is not None:
            need(expected == digest(existing), "pending_backup_conflict",
                 "Pending recovery inventory sidecar digest differs from its marker", run)
    elif marker.get("inventory_required"):
        raise Fault("pending_backup_incomplete", "Pending recovery inventory sidecar is missing", run)

    actual_by_path = {item["path"]: item for item in current["entries"]}
    merged_by_path = {}
    if existing is not None:
        for item in existing["entries"]:
            actual = actual_by_path.get(item["path"])
            if actual is None:
                # A restored special entry is intentionally metadata-only and
                # absent from the raw tree.  Regular payload absence is a
                # loss, so the next backup must refuse it.
                need(item["kind"] != "file", "pending_backup_incomplete",
                     "Pending regular payload is missing from the raw tree", item["path"])
                merged_by_path[item["path"]] = item
            else:
                need(_inventory_identity(item) == _inventory_identity(actual),
                     "pending_backup_conflict", "Pending inventory conflicts with the raw tree", item["path"])
                merged_by_path[item["path"]] = item
    for item in current["entries"]:
        merged_by_path.setdefault(item["path"], item)
    current["entries"] = sorted(merged_by_path.values(), key=lambda item: item["path"])
    prior_generation = 0
    if existing is not None and existing.get("provenance"):
        prior_generation = existing["provenance"]["generation"]
    current["provenance"] = {
        "generation": prior_generation + 1,
        "previous_digest": digest(existing) if existing is not None else None,
        "source": "validated_sidecar_and_raw_scan" if existing is not None else "raw_scan",
    }
    inventory = {
        **current,
    }
    _validate_inventory_shape(inventory, run)
    encoded = canonical(inventory)
    need(len(encoded) <= PENDING_INVENTORY_MAX_BYTES, "backup_capacity", "Pending recovery metadata inventory exceeds the explicit bound")
    return inventory, raw_files


class Operations:
    def __init__(self,service):self.c=service;self.s=service.s

    def doctor(self,actor):
        actor.require('owner')
        checks={'control_home':str(self.s.home.resolve()),'python':{'version':sys.version,'supported':sys.version_info >= (3,13)},
                'sqlite':{'version':sqlite3.sqlite_version,'foreign_keys':bool(self.s.one('PRAGMA foreign_keys')['foreign_keys']),
                          'integrity':self.s.one('PRAGMA integrity_check')['integrity_check']},
                'git':shutil.which('git'),'control_permissions':oct(self.s.home.stat().st_mode&0o777),
                'mode':self.c.g.mode,'execution_model':'cooperative-single-user','isolation_required':False,
                'platform':platform.platform(),'schema':SCHEMA_VERSION,'free_bytes':shutil.disk_usage(self.s.home).free,
                'audit':self.c.sec.audit(),'grammars':{},'adapters':[]}
        from .indexing import LANGUAGES
        for suffix,(name,_,_) in LANGUAGES.items():
            try:self.c.idx.parse('probe'+suffix,b'');checks['grammars'][name]='available'
            except Exception as exc:checks['grammars'][name]=str(exc)
        for row in self.s.all('SELECT name,qualified FROM adapters ORDER BY name'):
            try:
                adapter=self.c.rt.adapters.get(row['name']);checks['adapters'].append({'name':row['name'],'kind':adapter['kind'],'qualified':bool(row['qualified']),'integrity':'verified'})
            except Fault as exc:checks['adapters'].append({'name':row['name'],'error':exc.as_dict()})
        checks['formal_certification_possible']=checks['mode']=='governed' and any(a.get('qualified') for a in checks['adapters'])
        checks['limits']=['All processes share one user; no nested container, UID switch, sandbox or access control.',
                          'Malicious modification of the controller by a same-user process is outside scope.',
                          'Semantic review is a recorded judgment, not a mathematical proof of correctness.',
                          'Local disks only; sharing the control directory over a network filesystem is unsupported.']
        return checks

    def reconcile_startup(self):
        """Never infer success for processes lost during controller restart."""
        with self.s.transaction():
            for run in self.s.all("SELECT * FROM runs WHERE status IN ('registered','running')"):
                self.s.execute("UPDATE runs SET status='unknown',end=?,result=? WHERE id=?",(timestamp(),canonical({'reason':'controller_restarted_before_observed_completion'}).decode(),run['id']))
                if run['task']:
                    self.s.execute("UPDATE tasks SET status='planned',validity='needs_review',epoch=epoch+1,lease_owner=NULL,lease_until=NULL WHERE id=?",(run['task'],))
                    self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(run['task'],'run_unknown',run['id'],'Reconcile interrupted execution before retry'))
                self.c.g.inbox(run['project'],'run_unknown',run['id'],{'run':run['id'],'requires_reconciliation':True},'warning')
                self.c.sec.event(run['project'],'interrupted_execution_fenced','recovery',{'run':run['id']})
            for row in self.s.all("SELECT * FROM jobs WHERE status='running'"):
                # Traceability extraction publishes its immutable revision and
                # proposal result in one transaction before the worker records
                # the job terminal state.  A process exit in that narrow
                # window is therefore safely reconciled from the published
                # rows, without creating a second revision or inferring
                # success from a staging marker.
                reconciled = False
                if row['kind']=='traceability.extract':
                    try:
                        args=parse_json(row['args'])
                        proposal_id=args.get('proposal') if isinstance(args,dict) else None
                        proposal=self.s.one('SELECT * FROM traceability_proposals WHERE id=? AND project=?',(proposal_id,row['project'])) if proposal_id else None
                        result=parse_json(proposal['result']) if proposal and proposal.get('result') else None
                        revision_id=result.get('revision') if isinstance(result,dict) else None
                        revision=self.s.one('SELECT * FROM traceability_revisions WHERE id=? AND project=?',(revision_id,row['project'])) if revision_id else None
                        reconciled=bool(proposal and proposal['status']=='ready' and isinstance(result,dict)
                                        and result.get('status')=='ready' and revision and revision['status']=='ready'
                                        and result.get('revision_digest')==revision['digest']
                                        and result.get('population_digest')==revision['population_digest'])
                        if reconciled:
                            now=timestamp()
                            self.s.execute("UPDATE jobs SET status='succeeded',ended=?,result=?,error=NULL,retry_due=NULL WHERE id=?",
                                           (now,canonical(result).decode(),row['id']))
                            self.s.execute("UPDATE job_attempts SET status='succeeded',ended=?,result=?,error=NULL WHERE job=? AND status='running'",
                                           (now,canonical(result).decode(),row['id']))
                            self.c.sec.event(row['project'],'traceability_job_reconciled','recovery',
                                             {'job':row['id'],'proposal':proposal_id,'revision':revision_id,
                                              'idempotent':True,'fresh_review_or_test_evidence':False})
                    except (Fault, KeyError, TypeError, ValueError):
                        reconciled=False
                if not reconciled:
                    self.s.execute("UPDATE jobs SET status='unknown',ended=?,error=? WHERE id=?",(timestamp(),canonical({'code':'restart','message':'Execution was interrupted; not automatically retried'}).decode(),row['id']))
            self.s.execute("UPDATE job_attempts SET status='unknown',ended=?,error=? WHERE status='running'",(timestamp(),canonical({'code':'restart','message':'Interrupted attempt; side effects require reconciliation'}).decode()))
            self.c.rt.ledger.interrupted()
            self.s.execute("UPDATE tokens SET revoked=1 WHERE role IN ('worker','reviewer')")
        # Failed collector trees are reconciled independently of workflow
        # recovery.  This only appends retention evidence and never creates a
        # receipt, candidate, retry, or completion result.
        self.c.rt.reconcile_retention()
        return self.c.w.reconcile(Actor('recovery','owner'))

    def backup(self,actor):
        actor.require('owner');ident=uid('BACKUP');out=self.s.home/'exports'/f'{ident}.zip';out.parent.mkdir(exist_ok=True,mode=0o700)
        with self.s.lock:
            validate_origin_store(self.s)
            # Existing recipes and their physical closures are part of the
            # input contract.  Refuse a malformed closure before publishing a
            # new operational backup; GC applies the same fail-closed rule.
            validate_all_recipes(self.s, verify_artifact=True)
            self.c.sec.event(None,'backup_started',actor.id,{'id':ident})
            with tempfile.TemporaryDirectory(dir=self.s.home) as tmp:
                database=Path(tmp)/'state.sqlite3';self.s.backup_database(database)
                files={'state.sqlite3':database}
                pending_expectations={}
                if self.c.sec.keyfile.exists(): files['keys.json']=self.c.sec.keyfile
                for f in self.s.blobs.rglob('*'):
                    if f.is_file():files[f.relative_to(self.s.home).as_posix()]=f
                recipe_root=self.s.backup_recipes
                if recipe_root.exists():
                    need(recipe_root.is_dir() and not recipe_root.is_symlink(),
                         'backup_integrity','Backup recipe namespace is not a real directory')
                    for f in recipe_root.rglob('*'):
                        if f.is_file():
                            need(not f.is_symlink(), 'backup_integrity', 'Backup recipe member is a symlink', str(f))
                            name=f.relative_to(self.s.home).as_posix()
                            need(name not in files, 'backup_conflict', 'Backup member path is duplicated', name)
                            files[name]=f
                for folder in ('git','provider-secrets'):
                    root=self.s.home/folder
                    if root.exists():
                        for f in root.rglob('*'):
                            if f.is_file() and not f.is_symlink():files[f.relative_to(self.s.home).as_posix()]=f
                # Pending markers and their raw staged trees are part of the
                # confidential operational backup.  A failed blob write can
                # leave bytes only in this tree, so copying the marker alone
                # would make recovery falsely appear complete.
                pending=self.s.home/'recovery'/'pending'
                if pending.exists():
                    need(pending.is_dir() and not pending.is_symlink(), 'pending_backup_incomplete',
                         'Pending recovery directory is not a real directory')
                    try:
                        with os.scandir(pending) as pending_entries:
                            marker_paths=[]
                            for entry in pending_entries:
                                if not entry.name.endswith('.json'):
                                    continue
                                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                                    raise Fault('pending_backup_incomplete', 'Pending recovery marker is not a regular file', entry.name)
                                marker_paths.append(Path(entry.path))
                    except OSError as exc:
                        raise Fault('pending_backup_incomplete', 'Pending recovery marker inventory is unreadable', str(exc))
                    for f in sorted(marker_paths):
                        try:
                            marker=parse_json(f.read_bytes(),limit=2*1024*1024)
                        except Exception as exc:
                            raise Fault('pending_backup_incomplete', 'Pending recovery marker cannot be read', {'path':str(f),'error':str(exc)})
                        need(isinstance(marker,dict) and marker.get('format') == 'daikibo.failed-artifact-pending.v1',
                             'pending_backup_incomplete', 'Pending recovery marker has an unsupported format', f.name)
                        need(marker.get('run') == f.name[:-5], 'pending_backup_incomplete',
                             'Pending recovery marker name does not match its run', f.name)
                        inventory, raw_files = _pending_recovery_inventory(self.s.home, marker)
                        marker_name=f.relative_to(self.s.home).as_posix()
                        if inventory is not None:
                            archived_marker=dict(marker)
                            archived_marker['inventory_required']=True
                            archived_marker['inventory_digest']=digest(inventory)
                            marker_copy=Path(tmp)/f'{marker["run"]}.marker.json'
                            atomic_write(marker_copy, canonical(archived_marker), mode=0o600)
                            files[marker_name]=marker_copy
                        else:
                            files[marker_name]=f
                        for name,path in raw_files:
                            if name in files and files[name] != path:
                                raise Fault('pending_backup_duplicate', 'Pending recovery member conflicts with another backup member', name)
                            files[name]=path
                        for item in inventory.get('entries', []) if inventory is not None else ():
                            if item.get('kind') == 'file':
                                pending_expectations[item['path']] = {'sha256':item['sha256'],'bytes':item['bytes']}
                        if inventory is not None:
                            inventory_path=Path(tmp)/f'{marker["run"]}{PENDING_INVENTORY_SUFFIX}'
                            atomic_write(inventory_path, canonical(inventory), mode=0o600)
                            inventory_name=f'recovery/pending/{marker["run"]}{PENDING_INVENTORY_SUFFIX}'
                            files[inventory_name]=inventory_path
                # Freeze every mutable payload before the ZIP is opened.  CAS
                # leaves already under blobs are immutable and can be reused
                # directly; all other inputs use fixed <=1MiB leaves and a
                # private temporary copy so the ZIP bytes have an exact source
                # mapping.
                payload_mapping={}
                frozen=Path(tmp)/'frozen-payloads';frozen.mkdir(mode=0o700)
                for ordinal,(name,path) in enumerate(sorted(files.items())):
                    direct=direct_physical_mapping(self.s,path)
                    if direct is not None:
                        payload_mapping[name]=direct
                        continue
                    frozen_path,mapping=freeze_file_to_leaves(self.s,path,frozen/f'{ordinal:08d}.payload')
                    files[name]=frozen_path;payload_mapping[name]=mapping
                manifest={'format':'daikibo.backup.v1','schema':SCHEMA_VERSION,'created':timestamp(),'audit':self.c.sec.audit(),'files':{},
                          'confidential':True,'contains_credentials':True}
                partial=out.with_suffix('.partial');total=0
                try:
                    with zipfile.ZipFile(partial,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                        for name,path in sorted(files.items()):
                            with path.open('rb') as source,z.open(name,'w',force_zip64=True) as destination:
                                record=copy_hashed(source,destination)
                            expected=pending_expectations.get(name)
                            if expected is not None:
                                need(record == expected, 'pending_backup_conflict',
                                     'Pending regular payload changed before backup completed', name)
                            manifest['files'][name]=record;total+=record['bytes']
                            need(total<=MAX_BACKUP_TOTAL_BYTES,'backup_capacity','Backup exceeds explicit total bound')
                        encoded=canonical(manifest)
                        need(len(encoded)<=MAX_BACKUP_MANIFEST_BYTES,'backup_capacity','Backup file manifest exceeds bound')
                        # The manifest is itself a payload leaf.  It is added
                        # after the payload records are complete and is never
                        # read back from a guessed ZIP representation.
                        manifest_blob=self.s.blob_put(encoded)
                        payload_mapping['manifest.json']=[{'blob':manifest_blob,'offset':0,'bytes':len(encoded)}]
                        z.writestr('manifest.json',encoded,compress_type=zipfile.ZIP_STORED)
                    with partial.open('rb') as completed:os.fsync(completed.fileno())
                    os.chmod(partial,0o600);os.replace(partial,out)
                finally:
                    partial.unlink(missing_ok=True)
                # The completed ZIP remains the source of truth for all
                # structural bytes.  Publishing a recipe is the artifact
                # commit; the ZIP itself is intentionally never added as one
                # more physical blob leaf.
                recipe=recipe_from_stored_zip(out,payload_mapping,self.s,ident)
                publish_recipe(self.s,recipe)
        h=recipe['artifact_sha256']
        return {'id':ident,'path':str(out),'blob':h,'sha256':h,'bytes':out.stat().st_size,
                'warning':'Backup can contain configured provider credentials and legacy evidence keys. Do not publish it.'}

    def audit(self,actor,all_blobs=False):
        actor.require('owner');chain=self.c.sec.audit();checks=[]
        for row in self.s.all('SELECT id FROM receipts'):
            try:self.c.g.receipt(row['id'])
            except Fault as exc:checks.append({'receipt':row['id'],'error':exc.as_dict()})
        if all_blobs:
            for p in self.s.blobs.rglob('*'):
                if p.is_file():
                    expected=p.parent.name+p.name
                    if file_hash(p)!=expected:checks.append({'blob':expected,'error':'digest_mismatch'})
        for task in self.s.all("SELECT id,project,candidate FROM tasks WHERE status='completed'"):
            try:
                result=self.c.g.evaluate_task(actor,task['id'],gate='recheck')
                need(result['verdict']=='pass','completion_evidence_invalid','Full completion gate no longer passes',result['failures'])
            except Fault as exc:
                checks.append({'task':task['id'],'error':exc.as_dict()})
                with self.s.transaction():
                    self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1 WHERE id=? AND validity='current'",(task['id'],))
                    self.c.g.inbox(task['project'],'integrity_mismatch',task['id'],{'error':exc.as_dict()},'critical')
        return {'audit':chain,'problems':checks,'integrity_verified':not checks,'independent_rollback_anchor_required':True}

    def reconcile_evidence(self,batch_size=100):
        """Bounded, restartable sweep of actual facts. Not a substitute for final gate checks."""
        need(type(batch_size) is int and 1<=batch_size<=1000,'invalid_batch','Audit batch must be 1..1000')
        problems=[];scanned={}
        for table in ('receipts','tasks'):
            key='reconcile.cursor.'+table;stored=self.s.one('SELECT value FROM meta WHERE key=?',(key,))
            cursor=int(stored['value']) if stored else 0
            filter_sql="AND status='completed' AND validity='current'" if table=='tasks' else ''
            rows=self.s.all(f'SELECT rowid AS cursor,* FROM {table} WHERE rowid>? {filter_sql} ORDER BY rowid LIMIT ?',(cursor,batch_size))
            if not rows and cursor:
                cursor=0;rows=self.s.all(f'SELECT rowid AS cursor,* FROM {table} WHERE rowid>? {filter_sql} ORDER BY rowid LIMIT ?',(0,batch_size))
            for row in rows:
                try:
                    if table=='receipts':self.c.g.receipt(row['id'])
                    else:
                        need(not self.c.g.check_current(row['id']),'stale_context','Completed work has changed inputs or unresolved blocks')
                        self.c.g.implementation_evidence(row['id']);binding=self.c.g.task_binding(row['id'])
                        roles=list(self.c.g.policy(row['project'])['body']['review_roles'])
                        if parse_json(row['body']).get('risk')=='critical':roles+=self.c.g.policy(row['project'])['body']['critical_review_roles']
                        for role in roles:
                            refs=self.c.g.evidence_for(row['id'],binding,role)
                            need(refs,'missing_evidence','Required observed review is absent')
                            self.c.g.require_review(refs[0]['id'],row['id'],binding,{role})
                        plan=self.s.one('SELECT body FROM plans WHERE task=?',(row['id'],),True)
                        candidate=self.s.one('SELECT body FROM candidates WHERE id=?',(row['candidate'],),True) if row['candidate'] else None
                        candidate_snapshot=None
                        if candidate:
                            candidate_body=parse_json(candidate['body'])
                            snapshot=candidate_body.get('snapshot')
                            candidate_snapshot=snapshot.get('digest') if isinstance(snapshot,dict) else None
                        selection=self.c.g.task_test_evidence(
                            None, row['id'], binding=binding, snapshot_digest=candidate_snapshot)
                        selected={item['check_id']:item for item in selection['checks']}
                        for check in parse_json(plan['body'])['checks']:
                            item=selected.get(check['id'])
                            need(item is not None,'missing_evidence','Measured test receipt is absent')
                            need(item['status']=='executed','test_failed','Completed task test no longer supports completion',item)
                except Fault as exc:
                    problem={'table':table,'id':row['id'],'error':exc.as_dict()};problems.append(problem)
                    with self.s.transaction():
                        if table=='tasks':self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1 WHERE id=? AND validity='current'",(row['id'],))
                        self.c.g.inbox(row['project'],'integrity_mismatch',row['id'],problem,'critical')
                cursor=row['cursor']
            with self.s.transaction():self.s.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(cursor)))
            scanned[table]=len(rows)
        report={'scanned':scanned,'problems':problems,'batch_size':batch_size,'whole_database_certified':False,'at':timestamp()}
        with self.s.transaction():
            self.s.execute("INSERT INTO meta VALUES('reconcile.last_report',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(canonical(report).decode(),))
            if problems:self.c.sec.event(None,'periodic_integrity_findings','reconciler',report)
        return report

    def garbage_collect(self,actor,dry_run=True,minimum_age=86400):
        actor.require('owner');need(type(dry_run) is bool and minimum_age>=86400,'invalid_retention','Minimum safe retention is one day')
        need(not self.s.one("SELECT id FROM jobs WHERE status='running'"),'busy','Retention does not run while jobs execute')
        referenced=set()
        with self.s.lock:
            validate_origin_store(self.s)
            # Every committed recipe is a conservative root until an explicit
            # export-retention policy exists.  Validate all closures before
            # collecting anything, including in dry-run mode.
            for recipe in validate_all_recipes(self.s, verify_artifact=True):
                referenced.update(recipe.physical_refs)
            tables=[r['name'] for r in self.s.all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                need(re.fullmatch('[A-Za-z0-9_]+',table),'invalid_schema','Unexpected table identifier')
                for row in self.s.all(f'SELECT * FROM "{table}"'):
                    for v in row.values():
                        if isinstance(v,str):referenced.update(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',v))
            # Assurance references are an indexed closure root.  Keep this
            # explicit even though the conservative legacy scan above also
            # sees their digest columns; future JSON bodies must not be the
            # only route by which a historical proof remains live.
            if hasattr(self.c, 'assurance'):
                for project_row in self.s.all('SELECT id FROM projects ORDER BY id'):
                    referenced.update(self.c.assurance.cas_closure(project_row['id']))
            # A crash can leave a marker before its receipt transaction.  Its
            # manifest/chunk references remain live until reconciliation.
            pending=self.s.home/'recovery'/'pending'
            if pending.exists():
                for marker in pending.glob('*.json'):
                    if marker.is_file() and not marker.is_symlink():
                        referenced.update(referenced_blob_hashes(marker))
            # References inside content-addressed manifests must also be followed.
            queue=list(referenced)
            while queue:
                h=queue.pop();path=self.s.blobs/h[:2]/h[2:]
                if not path.is_file():continue
                for ch in referenced_blob_hashes(path):
                    if ch not in referenced:referenced.add(ch);queue.append(ch)
            candidates=[]
            for p in self.s.blobs.rglob('*'):
                if p.is_file() and p.parent.name+p.name not in referenced and p.stat().st_mtime<timestamp()-minimum_age:
                    candidates.append({'blob':p.parent.name+p.name,'bytes':p.stat().st_size})
                    if not dry_run:
                        p.unlink()
                        manager = getattr(self.s, '_artifact_manager', None)
                        if manager is not None:
                            manager.bump()
            if not dry_run:self.c.sec.event(None,'orphan_blobs_removed',actor.id,{'items':candidates})
        return {'dry_run':dry_run,'candidates':candidates,'retention_seconds':minimum_age,'history_and_evidence_never_deleted':True}


def _safe_restore_member(target, relative, field):
    target = Path(target).absolute()
    need(isinstance(relative, str) and relative, 'backup_corrupt',
         'Recovery inventory path is not a string', field)
    member = PurePosixPath(relative)
    need(not member.is_absolute()
         and "\\" not in relative and ".." not in member.parts,
         'backup_corrupt', 'Recovery inventory contains an unsafe relative path', field)
    path = target.joinpath(*member.parts)
    need(path.parent == target or path.is_relative_to(target), 'backup_corrupt',
         'Recovery inventory path escapes the restored home', field)
    return path


def _marker_needs_raw(marker):
    state = marker.get('state')
    summary = marker.get('summary') or {}
    return state in {'running','pending','pending_staging','staging','marker_error'} or summary.get('status') != 'complete'


def _restore_pending_inventory(target, marker, inventory, physical_target=None):
    """Validate a sidecar inventory and bind it to the new control home."""
    target = Path(target).absolute()
    physical_target = Path(physical_target or target).absolute()
    need(inventory.get('format') == PENDING_INVENTORY_FORMAT and inventory.get('run') == marker.get('run'),
         'backup_corrupt', 'Pending recovery inventory does not match its marker')
    expected_digest = marker.get('inventory_digest')
    need(expected_digest is None or expected_digest == digest(inventory), 'backup_corrupt',
         'Pending recovery inventory does not match its marker digest')
    roots = inventory.get('roots')
    need(isinstance(roots, dict), 'backup_corrupt', 'Pending recovery inventory roots are malformed')
    raw_root = inventory.get('raw_root')
    need(raw_root in {'worker_root','staged_root'} and raw_root in roots and 'work_root' in roots,
         'backup_corrupt', 'Pending recovery inventory has no raw root')
    for key, relative in roots.items():
        need(key in {'worker_root','staged_root','work_root'} and isinstance(relative, str) and relative,
             'backup_corrupt', 'Pending recovery inventory root is malformed', key)
    need(PurePosixPath(roots['work_root']) == PurePosixPath(roots[raw_root]) / 'work',
         'backup_corrupt', 'Pending recovery work root is not structurally bound')
    root_paths = {}
    for key, relative in roots.items():
        need(key in {'worker_root','staged_root','work_root'}, 'backup_corrupt',
             'Pending recovery inventory has an unknown root', key)
        path = _safe_restore_member(physical_target, relative, key)
        if path.exists() or path.is_symlink():
            need(path.is_dir() and not path.is_symlink(), 'backup_corrupt',
                 'Pending recovery root collides with a non-directory', key)
        else:
            path.mkdir(parents=True, mode=0o700)
        root_paths[key] = path
    need('work_root' in root_paths, 'backup_corrupt', 'Pending recovery inventory has no work root')
    entries = inventory.get('entries')
    need(isinstance(entries, list), 'backup_corrupt', 'Pending recovery inventory entries are malformed')
    seen = set()
    for item in entries:
        need(isinstance(item, dict), 'backup_corrupt', 'Pending recovery metadata entry is malformed')
        relative = item.get('path');root_key = item.get('root')
        need(root_key in root_paths, 'backup_corrupt', 'Pending recovery metadata has an unknown root', root_key)
        path = _safe_restore_member(physical_target, relative, 'entry')
        root = root_paths[root_key]
        need(path.is_relative_to(root), 'backup_corrupt', 'Pending recovery metadata escapes its root', relative)
        need(relative not in seen, 'backup_corrupt', 'Pending recovery metadata contains duplicates', relative)
        seen.add(relative)
        kind = item.get('kind')
        need(kind in {'file','symlink','fifo','socket','character_device','block_device','other'},
             'backup_corrupt', 'Pending recovery metadata kind is unsupported', kind)
        if kind == 'file':
            need(path.is_file() and not path.is_symlink(), 'backup_corrupt',
                 'Pending regular payload is missing from the restored backup', relative)
            observed = {'sha256': file_hash(path), 'bytes': path.stat().st_size}
            need(observed == {'sha256': item.get('sha256'), 'bytes': item.get('bytes')},
                 'backup_corrupt', 'Pending regular payload hash differs', relative)
            continue
        if kind == 'symlink':
            need(isinstance(item.get('target'), str), 'backup_corrupt', 'Pending symlink metadata has no target', relative)
        # Recreate parent directories so the reconciler can inspect the exact
        # metadata inventory even though it never follows or creates special
        # executable filesystem objects from a backup.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        need(not path.exists() and not path.is_symlink(), 'backup_corrupt',
             'Pending special metadata collides with a restored regular member', relative)
    for repo in marker.get('repos') or []:
        name = repo.get('name') if isinstance(repo, dict) else None
        need(isinstance(name, str) and name and len(PurePosixPath(name).parts) == 1
             and name not in {'.','..'} and '/' not in name and '\\' not in name,
             'backup_corrupt', 'Pending recovery repository name is unsafe', name)
        (root_paths['work_root'] / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for key in ('worker_root','staged_root'):
        relative = roots.get(key)
        marker[key] = str(target / PurePosixPath(relative)) if relative is not None else None
    marker['work_root'] = str(target / PurePosixPath(roots['work_root']))
    marker['inventory_required'] = True
    marker['inventory_digest'] = digest(inventory)


def _register_restored_backup_recipe(source, expected_sha256, staging, names, manifest_payload):
    """Register a new-format archive before restore mutates its staged DB.

    Legacy DEFLATED backups retain their historical path/blob behavior.  A
    ZIP_STORED backup can be represented exactly from the bytes already
    verified into ``staging``; mutable state bytes are leaf-frozen before token
    revocation or provider-path rebinding changes the staged copy.
    """
    with zipfile.ZipFile(source) as archive:
        if not archive.infolist() or any(info.compress_type != zipfile.ZIP_STORED for info in archive.infolist()):
            return None
    artifact_store = DirectoryArtifactStore(staging)
    validate_all_recipes(artifact_store, verify_artifact=True)
    mapping = {}
    with tempfile.TemporaryDirectory(dir=Path(staging).parent) as frozen_root:
        manifest_source = Path(frozen_root) / '.manifest.json'
        with manifest_source.open('wb') as stream:
            stream.write(manifest_payload)
        for ordinal, name in enumerate(sorted(names)):
            path = manifest_source if name == 'manifest.json' else Path(staging) / name
            direct = direct_physical_mapping(artifact_store, path)
            if direct is not None:
                mapping[name] = direct
                continue
            _, mapping[name] = freeze_file_to_leaves(artifact_store, path, Path(frozen_root) / f'{ordinal:08d}.payload')
        recipe = recipe_from_stored_zip(source, mapping, artifact_store, f'RESTORED-{expected_sha256}')
        publish_recipe(artifact_store, recipe)
    return recipe


def restore_backup(archive,home,expected_sha256):
    """Offline trusted-owner command; fails closed on traversal, extras and corruption."""
    source=Path(archive);target=Path(home).absolute()
    need(not target.exists(),'destination_exists','Restore must target a new directory')
    need(file_hash(source)==expected_sha256,'backup_mismatch','Compare the archive digest obtained from the original trusted control plane')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        staging=Path(temp)/'restore';staging.mkdir(mode=0o700)
        with zipfile.ZipFile(source) as z:
            names=z.namelist();need(len(names)==len(set(names)),'invalid_backup','Duplicate ZIP members')
            need('manifest.json' in names,'invalid_backup','No manifest')
            need(z.getinfo('manifest.json').file_size<=MAX_BACKUP_MANIFEST_BYTES,'backup_capacity','Manifest exceeds explicit bound')
            manifest_payload=z.read('manifest.json')
            manifest=parse_json(manifest_payload,limit=MAX_BACKUP_MANIFEST_BYTES)
            need(manifest['format']=='daikibo.backup.v1' and manifest['schema']<=SCHEMA_VERSION,'unsupported_backup','Wrong format or newer schema')
            need(set(names)==set(manifest['files'])|{'manifest.json'},'invalid_backup','Unexpected files')
            total=0
            for name,record in manifest['files'].items():
                path=PurePosixPath(name);info=z.getinfo(name)
                need(not path.is_absolute() and '..' not in path.parts and '\\' not in name and not stat.S_ISLNK(info.external_attr>>16),'unsafe_backup','Unsafe archive path')
                need(type(record['bytes']) is int and 0<=info.file_size==record['bytes']<=MAX_BACKUP_FILE_BYTES,'unsafe_backup','Unexpected file size')
                total+=info.file_size
                need(total<=MAX_BACKUP_TOTAL_BYTES,'backup_capacity','Expanded backup exceeds explicit total bound')
                destination=staging/name;destination.parent.mkdir(parents=True,exist_ok=True)
                with z.open(name) as incoming,destination.open('wb') as outgoing:
                    observed=copy_hashed(incoming,outgoing,record['bytes'])
                    outgoing.flush();os.fsync(outgoing.fileno())
                need(observed==record,'backup_corrupt','File hash or size differs',name)
        # Validate prior recipes and, for a new ZIP_STORED archive, register
        # this archive's exact bytes before the staged database is mutated.
        # A failure here leaves the destination unpublished and does not
        # rewrite any restored recipe or physical leaf.
        _register_restored_backup_recipe(source, expected_sha256, staging, names, manifest_payload)
        conn=sqlite3.connect(staging/'state.sqlite3')
        try:
            need(conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup_corrupt','Database integrity check failed')
            schema = conn.execute('PRAGMA user_version').fetchone()[0]
            if schema == SCHEMA_VERSION:
                validate_origin_database(conn)
            else:
                need(schema < SCHEMA_VERSION, 'unsupported_backup',
                     'Restored database schema is newer than the running program')
            from .db import Store
            from .domain_responsibility import validate_domain_store
            candidate_store = Store(staging)
            try:
                validate_domain_store(candidate_store)
            finally:
                candidate_store.close()
            conn.execute('UPDATE tokens SET revoked=1')
            # Protected provider secrets move with a restored vault; never retain the old absolute location.
            for name,secret_path in conn.execute('SELECT name,secret_path FROM providers').fetchall():
                if secret_path:conn.execute('UPDATE providers SET secret_path=? WHERE name=?',(str(target/'provider-secrets'/Path(secret_path).name),name))
            conn.commit()
        finally:conn.close()
        # Marker paths are absolute in the running home.  The backup sidecar
        # carries control-home-relative bindings and special-entry metadata;
        # restore never guesses from an ancestor component named "recovery".
        pending_dir=staging/'recovery'/'pending'
        if pending_dir.is_dir():
            inventories={}
            for inventory_path in pending_dir.glob(f'*{PENDING_INVENTORY_SUFFIX}'):
                need(inventory_path.is_file() and not inventory_path.is_symlink(), 'backup_corrupt',
                     'Pending recovery inventory is not a regular file', inventory_path.name)
                inventory=parse_json(inventory_path.read_bytes(),limit=PENDING_INVENTORY_MAX_BYTES)
                need(isinstance(inventory,dict) and inventory.get('format') == PENDING_INVENTORY_FORMAT,
                     'backup_corrupt', 'Pending recovery inventory format differs', inventory_path.name)
                run=inventory.get('run')
                need(isinstance(run,str) and run and inventory_path.name == run + PENDING_INVENTORY_SUFFIX,
                     'backup_corrupt', 'Pending recovery inventory name does not match its run', inventory_path.name)
                need(run not in inventories, 'backup_corrupt', 'Duplicate pending recovery inventory', run)
                inventories[run]=inventory
            marker_paths=sorted(pending_dir.glob('*.json'))
            for marker_path in marker_paths:
                need(marker_path.is_file() and not marker_path.is_symlink(), 'backup_corrupt',
                     'Pending recovery marker is not a regular file', marker_path.name)
                marker=parse_json(marker_path.read_bytes(),limit=2*1024*1024)
                need(isinstance(marker,dict) and marker.get('format') == 'daikibo.failed-artifact-pending.v1',
                     'backup_corrupt', 'Pending recovery marker format differs', marker_path.name)
                run=marker.get('run')
                need(isinstance(run,str) and run and marker_path.name == run + '.json',
                     'backup_corrupt', 'Pending recovery marker name does not match its run', marker_path.name)
                inventory=inventories.pop(run,None)
                if inventory is not None:
                    _restore_pending_inventory(target, marker, inventory, physical_target=staging)
                else:
                    # A marker with raw pending evidence cannot be restored
                    # from an older archive that omitted its inventory: doing
                    # so would silently lose bytes or special metadata.
                    need(not _marker_needs_raw(marker) and not any(marker.get(key) for key in ('worker_root','work_root','staged_root')),
                         'backup_incomplete', 'Pending recovery inventory is missing; restore refused')
                atomic_write(marker_path,canonical(marker),mode=0o600)
            need(not inventories, 'backup_corrupt', 'Pending recovery inventory has no marker')
        staging.rename(target)
    return {'home':str(target),'restored':True,'next':'Start the controller; interrupted runs require reconciliation.'}
