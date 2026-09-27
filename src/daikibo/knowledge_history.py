"""Self-contained specification history and repairable generated Git baselines.

Exports retain recorded facts, not fresh engineering certification. Operational
restore (tasks, receipts, keys, jobs) is intentionally the separate full backup API.
"""
from __future__ import annotations

import base64
import binascii
import os
import tempfile
import zipfile
from pathlib import Path

from .common import Fault, canonical, digest, need, number, parse_json, text, timestamp, uid
from .gitops import Snapshots, git
from .program_origins import validate_origin_rows, validate_origin_store
from .portable_context import (
    MAX_SNAPSHOT_BYTES, PortableObservedContext,
    make_observed_context, required_cas_refs,
)

FORMAT='daikibo.knowledge-snapshot.v1'
SPEC_FORMATS={'daikibo.spec.v2','daikibo.spec.v3','daikibo.spec.v4','daikibo.spec.v5','daikibo.spec.v6','daikibo.spec.v7'}


def decoded(row):
    value=dict(row)
    for key in ('body','config','refs'):
        if key in value and isinstance(value[key],str):value[key]=parse_json(value[key],limit=MAX_SNAPSHOT_BYTES)
    return value


def _observed_external_rows(spec):
    """Return non-wire rows needed by the shared assurance endpoint reader."""
    history = spec.get('assurance_history') or {}
    need(isinstance(history, dict), 'invalid_snapshot',
         'Specification assurance history is malformed')

    def rows(name):
        value = spec.get(name, [])
        need(isinstance(value, list), 'invalid_snapshot',
             f'Specification {name} rows are malformed')
        return value

    assurance_objects = history.get('assurance_objects', [])
    need(isinstance(assurance_objects, list), 'invalid_snapshot',
         'Specification assurance object rows are malformed')
    return {
        'artifacts': rows('artifacts'),
        'revisions': rows('revisions'),
        'sources': rows('sources'),
        'changes': rows('changes'),
        'task_revision_history': rows('task_revision_history'),
        'assurance_objects': assurance_objects,
        'traceability_items': spec.get('traceability_history', {}).get('traceability_items', []),
    }


def _validate_observed_context(spec):
    """Build the wire-only reader and prove its exact typed CAS closure."""
    reader = PortableObservedContext.from_wire(
        spec.get('observed_context'), project=spec['project'],
        extra_rows=_observed_external_rows(spec), code='invalid_snapshot')
    required = required_cas_refs(spec, reader.rows, reader)
    provided = set(reader.blob_ids)
    need(required == provided, 'invalid_snapshot',
         'Observed context CAS closure differs from its manifest', {
             'missing': sorted(required - provided),
             'extra': sorted(provided - required),
         })
    return reader


def _validate_specifications(spec, *, include_projection=False):
    need(isinstance(spec,dict) and spec.get('format') in SPEC_FORMATS,'invalid_snapshot','Unsupported specification export')
    try:
        encoded_spec = canonical(spec)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Fault('invalid_snapshot', 'Specification is not canonical JSON') from exc
    if len(encoded_spec) > MAX_SNAPSHOT_BYTES:
        raise Fault('snapshot_too_large', 'Specification export exceeds the explicit size limit', {
            'kind': 'specification_bytes', 'required': len(encoded_spec),
            'limit': MAX_SNAPSHOT_BYTES,
        })
    need(spec['project_record']['id']==spec['project'],'invalid_snapshot','Project record differs')
    planning_history = spec.get('planning_history')
    need(isinstance(planning_history, dict), 'invalid_snapshot',
         'Specification planning history is malformed')
    planning_format = planning_history.get('format')
    if spec['format'] in {'daikibo.spec.v5','daikibo.spec.v6','daikibo.spec.v7'}:
        need(planning_format=='daikibo.planning-history.v2','invalid_snapshot',
             'Specification v5/v6 requires planning history v2')
    else:
        need(planning_format=='daikibo.planning-history.v1','invalid_snapshot',
             'Older specification requires planning history v1')
    observed_reader = None
    if spec['format'] in {'daikibo.spec.v6','daikibo.spec.v7'}:
        observed_reader = _validate_observed_context(spec)
    else:
        need('observed_context' not in spec, 'invalid_snapshot',
             'Observed context requires specification v6')
    if 'assurance_history' in spec:
        from .assurance import validate_assurance_rows
        history=spec['assurance_history']
        need(set(history) >= {'assurance_objects','assurance_events','assurance_heads','assurance_refs'},
             'invalid_snapshot','Assurance history sections are incomplete')
        assurance_tables={key: history[key] for key in ('assurance_objects','assurance_events','assurance_heads','assurance_refs')}
        validate_assurance_rows(assurance_tables, spec['project'],
                                external_tables=observed_reader,
                                blob_get=observed_reader.blob if observed_reader is not None else None)
    from .domain_responsibility import required_contracts, validate_contract_manifest
    history = spec.get('assurance_history') or {}
    runs = observed_reader.rows.get('runs', []) + observed_reader.rows.get('receipts', []) if observed_reader else []
    expected_contracts = required_contracts(history.get('assurance_objects', []), runs)
    validate_contract_manifest(history.get('required_contracts'), expected_contracts,
        modern=spec['format']=='daikibo.spec.v7', code='invalid_snapshot')
    if spec['format']=='daikibo.spec.v7':
        need(history.get('format')=='daikibo.assurance-history.v2' and history.get('history_version')==2,
             'invalid_snapshot', 'Specification v7 requires assurance history v2')
    elif history:
        need(history.get('format')=='daikibo.assurance-history.v1' and history.get('history_version')==1,
             'invalid_snapshot', 'Historical assurance contract differs')
    from .task_revisions import validate_history_record, validate_proposal_record
    for record in spec.get('task_revision_history',[]): validate_history_record(record,spec['project'])
    for record in spec.get('task_revision_proposals',[]): validate_proposal_record(record,spec['project'])
    if 'workstream_history' in spec:
        from .workstream_history import validate_history
        history=spec['workstream_history']
        tables={**spec['planning_history'],**history,**spec.get('scope_return_history',{})}
        tables['revisions']=spec['revisions']
        def historical_get(section,key):
            values=[r for r in tables[section] if (canonical([r['artifact'],r['revision']]).decode() if section=='revisions' else r['id'])==key]
            need(len(values)==1,'invalid_snapshot','Missing/duplicate delegated history record')
            return values[0]
        def historical_each(section,ref=None):
            values=tables[section]
            if ref is not None:values=[r for r in values if r.get('proposal' if section=='scope_return_packets' else 'scope')==ref]
            if section=='workstream_packets':values=sorted(values,key=lambda r:(r['scope'],r['ordinal']))
            return iter(values)
        for section,values in history.items():
            need(len({r['id'] for r in values})==len(values),'invalid_snapshot','Duplicate workstream history')
        validate_history(historical_get,historical_each,spec['project'])
        if 'scope_return_history' in spec:
            from .scope_return_history import validate_returns
            for rows in spec['scope_return_history'].values():
                need(len({r['id'] for r in rows})==len(rows),'invalid_snapshot','Duplicate return history')
            validate_returns(historical_get,historical_each,spec['project'])
    if 'subplan_history' in spec:
        from .subplan_history import validate_subplans
        history=spec['subplan_history']
        tables={**spec['planning_history'],**history,'revisions':spec['revisions'],'sources':spec['sources'],'artifacts':spec['artifacts']}
        def partial_get(section,key):
            found=[r for r in tables[section] if (canonical([r['artifact'],r['revision']]).decode() if section=='revisions' else r['id'])==key]
            need(len(found)==1,'invalid_snapshot','Missing/duplicate partial-plan history')
            return found[0]
        def partial_each(section,ref=None):
            rows=tables[section]
            if ref is not None:rows=[r for r in rows if r.get('subplan')==ref]
            if section=='subplan_packets':rows=sorted(rows,key=lambda r:(r['subplan'],r['ordinal']))
            return iter(rows)
        for rows in history.values():need(len({r['id'] for r in rows})==len(rows),'invalid_snapshot','Duplicate partial history')
        validate_subplans(partial_get,partial_each,spec['project'])
    local_counts={}
    if spec.get('format')=='daikibo.spec.v3':
        need('local_execution_history' in spec,'invalid_snapshot','Specification v3 requires local execution history')
    if 'local_execution_history' in spec:
        # v4 is the additive execution-control export and may retain the
        # complete local-execution history alongside its newer sections.
        # Keep v3 readable while rejecting omission from older formats.
        need(spec.get('format') in {'daikibo.spec.v3', 'daikibo.spec.v4', 'daikibo.spec.v5', 'daikibo.spec.v6', 'daikibo.spec.v7'},
             'invalid_snapshot','Local execution history requires specification v3 or v4')
        from .local_execution_history import validate_local_executions
        history=spec['local_execution_history']
        required_local={'local_execution_proposals','local_execution_packets','local_execution_records'}
        need(set(history)==required_local,'invalid_snapshot','Local execution history sections are incomplete')
        need('subplan_history' in spec,'invalid_snapshot','Local execution history requires retained subplan history')
        tables={**spec['planning_history'],**spec['subplan_history'],**history}
        def local_get(section,key):
            values=tables[section]
            found=[r for r in values if r.get('id')==key]
            need(len(found)==1,'invalid_snapshot','Missing/duplicate local execution history record')
            return found[0]
        def local_each(section,ref=None):
            values=list(tables[section])
            if ref is not None:
                field='proposal' if section in {'local_execution_packets','local_execution_records'} else 'subplan' if section=='subplan_packets' else None
                if field:values=[r for r in values if r.get(field)==ref]
            if section=='local_execution_packets':values.sort(key=lambda r:(r['proposal'],r['ordinal'],r['id']))
            elif section=='local_execution_records':values.sort(key=lambda r:(r['proposal'],r.get('created',0),r['id']))
            return iter(values)
        for section in required_local:
            rows=history[section]
            need(len({r['id'] for r in rows})==len(rows),'invalid_snapshot','Duplicate local execution history')
        local_counts=validate_local_executions(local_get,local_each,spec['project'])
    if spec.get('format')=='daikibo.spec.v4':
        need('execution_control_history' in spec,'invalid_snapshot','Specification v4 requires execution-control history')
    if 'execution_control_history' in spec:
        need(spec.get('format') in {'daikibo.spec.v4', 'daikibo.spec.v5', 'daikibo.spec.v6', 'daikibo.spec.v7'},'invalid_snapshot','Execution-control history requires specification v4, v5 or v6')
        from .execution_control_history import SECTIONS as EXECUTION_SECTIONS, validate_execution_controls
        history=spec['execution_control_history']
        need(set(history)==set(EXECUTION_SECTIONS),'invalid_snapshot','Execution-control history sections are incomplete')
        tables={**spec.get('planning_history',{}),**spec.get('subplan_history',{}),**spec.get('local_execution_history',{}),**history}
        def execution_get(section,key):
            rows=tables[section]
            found=[r for r in rows if r.get('id')==key]
            need(len(found)==1,'invalid_snapshot','Missing/duplicate execution-control history record')
            return found[0]
        def execution_each(section,ref=None):
            rows=list(tables[section])
            if ref is not None:
                field='proposal' if section in {'execution_control_packets','execution_control_events'} else 'task' if section in {'execution_attempts','attempt_assessments','execution_control_proposals','execution_control_authorizations'} else None
                if field: rows=[r for r in rows if r.get(field)==ref]
            if section in {'execution_attempts','attempt_assessments'}: rows.sort(key=lambda r:(r['task'],r['attempt_epoch'],r['id']))
            elif section in {'execution_control_proposals','execution_control_events'}: rows.sort(key=lambda r:(r.get('task',r.get('proposal')),r.get('created',0),r['id']))
            elif section=='execution_control_packets': rows.sort(key=lambda r:(r['proposal'],r['ordinal'],r['id']))
            elif section=='execution_control_authorizations': rows.sort(key=lambda r:(r['task'],r['control_revision'],r['id']))
            return iter(rows)
        for section in EXECUTION_SECTIONS:
            rows=history[section]
            need(len({r['id'] for r in rows})==len(rows),'invalid_snapshot','Duplicate execution-control history')
        execution_counts=validate_execution_controls(execution_get,execution_each,spec['project'])
        local_counts={**local_counts,**execution_counts}
    artifacts={a['id']:a for a in spec['artifacts']}
    need(len(artifacts)==len(spec['artifacts']),'invalid_snapshot','Duplicate artifact IDs')
    revisions={}
    for row in spec['revisions']:
        key=(row['artifact'],row['revision'])
        need(key not in revisions and row['artifact'] in artifacts,'invalid_snapshot','Duplicate or dangling revision')
        need(type(row['revision']) is int and 1<=row['revision']<=artifacts[row['artifact']]['revision'],'invalid_snapshot','Revision is outside recorded history')
        need(digest(row['body'])==row['digest'],'invalid_snapshot','Revision digest differs')
        revisions[key]=row
    for ident,artifact in artifacts.items():
        need(type(artifact['revision']) is int and artifact['revision']>0,'invalid_snapshot','Invalid current revision')
        need(artifact['project']==spec['project'],'invalid_snapshot','Artifact belongs to a different project')
        need(digest(artifact['body'])==artifact['digest'],'invalid_snapshot','Artifact digest differs')
        for revision in range(1,artifact['revision']+1):
            need((ident,revision) in revisions,'invalid_snapshot','Missing historical revision')
        current=revisions[(ident,artifact['revision'])]
        need(current['digest']==artifact['digest'],'invalid_snapshot','Current artifact and last revision disagree')
    sources={s['id']:s for s in spec['sources']}
    need(len(sources)==len(spec['sources']) and set(spec['source_contents'])==set(sources),'invalid_snapshot','Missing/duplicate source contents')
    for ident,row in sources.items():
        content=spec['source_contents'][ident]
        need(isinstance(content,str) and row['project']==spec['project'],'invalid_snapshot','Invalid source')
        need(digest(content.encode())==row['blob'] and len(content)==row['characters'],'invalid_snapshot','Source hash/length differs')
    from .archive_chunks import historical_source_reference_projection
    source_projection=historical_source_reference_projection(
        spec['revisions'], spec['artifacts'],
        lambda source: need(source in sources,'invalid_snapshot','Referenced source is missing from the archive'),
        error_code='invalid_snapshot')
    ranges={}
    for row in spec['dispositions']:
        need(row['source'] in sources,'invalid_snapshot','Disposition has no source')
        need(type(row['start']) is int and type(row['end']) is int and 0<=row['start']<row['end']<=sources[row['source']]['characters'],'invalid_snapshot','Invalid classified range')
        need(all(ref in artifacts for ref in row['refs']),'invalid_snapshot','Disposition references missing artifact')
        ranges.setdefault(row['source'],[]).append((row['start'],row['end']))
    for intervals in ranges.values():
        previous=0
        for start,end in sorted(intervals):
            need(start>=previous,'invalid_snapshot','Overlapping source classifications');previous=end
    for link in spec['links']:
        need(link['source'] in artifacts and link['target'] in artifacts,'invalid_snapshot','Dangling trace link')
    documents={d['id']:d for d in spec['documents']}
    need(len(documents)==len(spec['documents']) and set(documents)==set(spec['document_contents']),'invalid_snapshot','Missing or duplicate raw document bytes')
    for ident,record in documents.items():
        need(record['project']==spec['project'],'invalid_snapshot','Document belongs to a different project')
        data=base64.b64decode(spec['document_contents'][ident],validate=True)
        need(digest(data)==record['body']['raw_digest'] and len(data)==record['body']['bytes'],'invalid_snapshot','Raw document hash/length differs')
    registries={}
    for table in ('decisions','changes','conflicts'):
        values=spec[table];registered={r['id']:r for r in values}
        need(len(registered)==len(values),'invalid_snapshot','Duplicate '+table+' identifiers')
        need(all(r['project']==spec['project'] for r in values),'invalid_snapshot','Cross-project '+table)
        registries[table]=registered
    for decision in spec['decisions']:
        need(digest(decision['body'])==decision['digest'],'invalid_snapshot','Decision body digest differs')
        need(not decision.get('source') or decision['source'] in sources,'invalid_snapshot','Decision has no recorded source')
    need(len({r['id'] for r in spec['attempts']})==len(spec['attempts']),'invalid_snapshot','Duplicate attempt IDs')
    for attempt in spec['attempts']:
        need(attempt['change_id'] in registries['changes'],'invalid_snapshot','Attempt has no change')
    for conflict in spec['conflicts']:
        need(not conflict.get('decision') or conflict['decision'] in registries['decisions'],'invalid_snapshot','Conflict has no recorded decision')
    if 'planning_history' in spec:
        validate_planning_history(spec['planning_history'], spec['project'])
    counts={'artifacts':len(artifacts),'revisions':len(revisions),'sources':len(sources),
            'classifications':len(spec['dispositions']),'links':len(spec['links']),
            'documents':len(documents),'decisions':len(spec['decisions']),'changes':len(spec['changes']),
            **local_counts}
    return {'counts':counts,**source_projection} if include_projection else counts


def validate_planning_history(history, project):
    """Historical plan data are portable; old receipts are not new acceptance."""
    need(isinstance(history, dict) and history.get('format') in {
        'daikibo.planning-history.v1','daikibo.planning-history.v2',
    },
         'invalid_snapshot','Unsupported planning history')
    tables={}
    for table in ('programs','breakdowns','packets','closures'):
        rows=history[table];by_id={r['id']:r for r in rows}
        need(len(by_id)==len(rows) and all(r['project']==project for r in rows),'invalid_snapshot','Duplicate or cross-project planning history')
        tables[table]=by_id
    for row in tables['breakdowns'].values():
        need(row['program'] in tables['programs'] and digest(row['body'])==row['digest'],'invalid_snapshot','Invalid historical breakdown')
        need(not row['previous'] or row['previous'] in tables['breakdowns'],'invalid_snapshot','Missing previous breakdown')
    for row in tables['packets'].values():
        need(digest(row['body'])==row['digest'],'invalid_snapshot','Invalid historical review packet')
    members={};seen=set();ordinals=set()
    for member in history['members']:
        pair=(member['breakdown'],member['packet']);order=(member['breakdown'],member['ordinal'])
        need(pair not in seen and order not in ordinals,'invalid_snapshot','Duplicate packet membership')
        seen.add(pair);ordinals.add(order)
        need(pair[0] in tables['breakdowns'] and pair[1] in tables['packets'],'invalid_snapshot','Dangling review packet membership')
        packet=tables['packets'][pair[1]]['body']
        need(packet['program']==tables['breakdowns'][pair[0]]['program'],'invalid_snapshot','Packet belongs to another program')
        members.setdefault(pair[0],{}).setdefault(packet['unit'],[]).append(packet)
    for ident,row in tables['breakdowns'].items():
        expected=row['body']['material_bindings'];groups=members.get(ident,{})
        need(set(expected)==set(groups),'invalid_snapshot','Missing historical review unit')
        for unit,h in expected.items():
            parts=sorted(groups[unit],key=lambda v:v['start']);position=0;data=[]
            for part in parts:
                need(part['start']==position and part['material_digest']==h,'invalid_snapshot','Review fragment range mismatch')
                position=part['end'];data.append(part['serialized_fragment'])
            need(position==parts[-1]['total_characters'] and digest(''.join(data).encode())==h,'invalid_snapshot','Incomplete historical review material')
    for row in history['adoptions']:
        need(row['breakdown'] in tables['breakdowns'],'invalid_snapshot','Adoption refers to missing breakdown')
    for row in tables['closures'].values():
        need(row['program'] in tables['programs'],'invalid_snapshot','Closure refers to missing program')
    need(len({r['breakdown'] for r in history['adoptions']})==len(history['adoptions']),'invalid_snapshot','Duplicate adoption')
    if history['format']=='daikibo.planning-history.v2':
        need('program_origins' in history, 'invalid_snapshot',
             'Planning history v2 requires program origins')
        origin_counts=validate_origin_rows(
            tables['programs'].values(), history['program_origins'], project,
        )
    else:
        need('program_origins' not in history, 'invalid_snapshot',
             'Planning history v1 cannot carry program origins')
        origin_counts={}
    return {'programs':len(tables['programs']),'breakdowns':len(tables['breakdowns']),
            'packets':len(tables['packets']),**origin_counts}


def validate_specifications(spec, *, include_projection=False):
    """Corrupt or incompatible records are reported as a structured rejection."""
    try:
        return _validate_specifications(spec, include_projection=include_projection)
    except (AttributeError, KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise Fault('invalid_snapshot','Malformed specification record') from exc


def validate_payload(payload, *, include_projection=False):
    need(isinstance(payload,dict) and payload.get('format')==FORMAT,'invalid_snapshot','Unsupported baseline format')
    try:
        spec=payload['specifications'];baseline=payload['baseline']
        validation=validate_specifications(spec, include_projection=include_projection)
        projection=validation if include_projection else None
        counts=validation['counts'] if include_projection else validation
        need(baseline['project']==spec['project'],'invalid_snapshot','Baseline project differs')
        accepted=[{key:row[key] for key in ('id','kind','revision','digest')}
                  for row in spec['artifacts'] if row['status']=='accepted']
        need(baseline['artifacts']==accepted,'invalid_snapshot','Baseline accepted set differs from recorded specifications')
        return projection if include_projection else counts
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise Fault('invalid_snapshot','Malformed baseline record') from exc


class KnowledgeHistory:
    def __init__(self,store,security,knowledge):
        self.s,self.sec,self.k=store,security,knowledge

    def _execution_control_history_available(self, project):
        """Return whether this store has the additive schema-13 projection."""
        columns={row['name'] for row in self.s.all('PRAGMA table_info(tasks)')}
        if 'no_progress_count' not in columns:
            return False
        tables={row['name'] for row in self.s.all("SELECT name FROM sqlite_master WHERE type='table'")}
        required={'execution_attempts','attempt_assessments','execution_control_proposals',
                  'execution_control_packets','execution_control_events','execution_control_authorizations'}
        if not required <= tables:
            return False
        # Schema 13 is additive and may be installed before a project has any
        # execution-control records.  Keep the established v2/v3 export shape
        # for such projects; switch to v4 once immutable control history (or a
        # nonzero cached assessment counter or legacy attempt telemetry) exists
        # to preserve.  Do not synthesize schema-13 rows for that telemetry.
        return bool(self.s.one('''SELECT 1 FROM execution_attempts WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM attempt_assessments WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM execution_control_proposals WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM execution_control_packets WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM execution_control_events WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM execution_control_authorizations WHERE project=? LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM tasks WHERE project=? AND no_progress_count>0 LIMIT 1''',(project,))) or \
            bool(self.s.one('''SELECT 1 FROM tasks WHERE project=? AND attempts>0 LIMIT 1''',(project,)))

    def _collector_failure_history_available(self, project):
        """Detect durable collector-failure history without inventing rows."""
        if self.s.one("SELECT 1 FROM receipts WHERE project=? AND json_extract(body,'$.recovery_artifacts') IS NOT NULL LIMIT 1", (project,)):
            return True
        if self.s.one("SELECT 1 FROM events WHERE project=? AND kind IN ('retention_reconciled','retention_pending') LIMIT 1", (project,)):
            return True
        pending=self.s.home/'recovery'/'pending'
        if pending.is_dir():
            for path in pending.glob('*.json'):
                if path.is_symlink() or not path.is_file():continue
                try:
                    marker=parse_json(path.read_bytes(),limit=2*1024*1024)
                except Exception:
                    continue
                if isinstance(marker,dict) and marker.get('project')==project:
                    return True
        return False

    @staticmethod
    def _decode_history_row(table, row):
        value=decoded(row)
        if table in {'attempt_assessments','execution_control_authorizations'}:
            # ``assessment`` is the portable progress/no_progress enum on an
            # authorization.  Only the evidence column is JSON encoded.
            if value.get('evidence') is not None and isinstance(value['evidence'],str):
                value['evidence']=parse_json(value['evidence'],limit=MAX_SNAPSHOT_BYTES)
        return value

    def _execution_control_history(self, project):
        from .execution_control_history import SECTIONS
        return {table:[self._decode_history_row(table,row) for row in self.s.all(
                    f'SELECT * FROM {table} WHERE project=? ORDER BY id',(project,))]
                for table in SECTIONS}

    def export_current(self,actor,project):
        self.k.project(actor,project)
        with self.s.lock:
            validate_origin_store(self.s)
            has_local=bool(self.s.one('SELECT 1 FROM local_execution_proposals WHERE project=? LIMIT 1',(project,)))
            has_execution=self._execution_control_history_available(project)
            has_origins=True
            from .domain_responsibility import store_required_contracts
            required_contracts = store_required_contracts(self.s, project)
            has_assurance=bool(required_contracts) or bool(self.s.one('SELECT 1 FROM assurance_objects WHERE project=? LIMIT 1', (project,)))
            spec_format='daikibo.spec.v7' if required_contracts else 'daikibo.spec.v6' if has_assurance else 'daikibo.spec.v5'
            spec={'format':spec_format,'project':project,
                  'project_record':decoded(self.k.project(actor,project)),
                  'artifacts':[decoded(r) for r in self.s.all('SELECT * FROM artifacts WHERE project=? ORDER BY id',(project,))],
                  'revisions':[decoded(r) for r in self.s.all('SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=? ORDER BY r.artifact,r.revision',(project,))],
                  'links':self.s.all('SELECT l.* FROM links l JOIN artifacts a ON a.id=l.source WHERE a.project=? ORDER BY l.source,l.target,l.relation',(project,)),
                  'sources':self.s.all('SELECT * FROM sources WHERE project=? ORDER BY id',(project,)),
                  'dispositions':[decoded(r) for r in self.s.all('SELECT d.* FROM dispositions d JOIN sources s ON s.id=d.source WHERE s.project=? ORDER BY d.source,d.start',(project,))],
                  'authority':'read_only_export_not_runtime_state','runtime_restore_supported':False,
                  'fresh_review_or_test_evidence':False}
            for table in ('decisions','changes','conflicts','documents'):
                spec[table]=[decoded(r) for r in self.s.all(f'SELECT * FROM {table} WHERE project=? ORDER BY id',(project,))]
            programs=[decoded(r) for r in self.s.all('SELECT * FROM programs WHERE project=? ORDER BY id',(project,))]
            origin_rows=[decoded(r) for r in self.s.all('SELECT * FROM program_origins WHERE project=? ORDER BY program',(project,))]
            validate_origin_rows(programs, origin_rows, project, code='invalid_snapshot')
            spec['planning_history']={
                'format':'daikibo.planning-history.v2' if has_origins else 'daikibo.planning-history.v1',
                'programs':programs,
                'breakdowns':[decoded(r) for r in self.s.all('SELECT * FROM breakdowns WHERE project=? ORDER BY id',(project,))],
                'packets':[decoded(r) for r in self.s.all('SELECT * FROM breakdown_packets WHERE project=? ORDER BY id',(project,))],
                'members':self.s.all('SELECT m.* FROM breakdown_members m JOIN breakdowns b ON b.id=m.breakdown WHERE b.project=? ORDER BY m.breakdown,m.ordinal',(project,)),
                'adoptions':[decoded(r) for r in self.s.all('SELECT a.* FROM breakdown_adoptions a JOIN breakdowns b ON b.id=a.breakdown WHERE b.project=? ORDER BY a.breakdown',(project,))],
                'closures':[decoded(r) for r in self.s.all('SELECT * FROM program_closures WHERE project=? ORDER BY id',(project,))],
                'fresh_evidence':False,
            }
            if has_origins:
                spec['planning_history']['program_origins']=origin_rows
            spec['task_revision_proposals']=[decoded(r) | {'result':parse_json(r['result']) if r['result'] else None}
                for r in self.s.all('SELECT * FROM task_revision_proposals WHERE project=? ORDER BY task,from_revision,id',(project,))]
            spec['task_revision_history']=[decoded(r) for r in self.s.all('SELECT * FROM task_revision_history WHERE project=? ORDER BY task,to_revision',(project,))]
            spec['attempts']=[decoded(r) for r in self.s.all('SELECT a.* FROM attempts a JOIN changes c ON c.id=a.change_id WHERE c.project=? ORDER BY a.created,a.id',(project,))]
            spec['source_contents']={r['id']:self.s.blob_get(r['blob']).decode('utf-8') for r in spec['sources']}
            spec['document_contents']={r['id']:base64.b64encode(self.s.blob_get(r['body']['raw_digest'])).decode('ascii') for r in spec['documents']}
            if self.s.one('SELECT 1 FROM workstreams WHERE project=? LIMIT 1',(project,)):
                spec['workstream_history']={
                    'workstreams':[decoded(r) for r in self.s.all('SELECT * FROM workstreams WHERE project=? ORDER BY id',(project,))],
                    'workstream_packets':[decoded(r) for r in self.s.all('SELECT p.* FROM workstream_packets p JOIN workstreams w ON w.id=p.scope WHERE w.project=? ORDER BY p.scope,p.ordinal',(project,))],
                    'workstream_records':[decoded(r) for r in self.s.all('SELECT * FROM workstream_records WHERE project=? ORDER BY scope,created,id',(project,))],
                }
            if self.s.one('SELECT 1 FROM scope_returns WHERE project=? LIMIT 1',(project,)):
                spec['scope_return_history']={
                    'scope_returns':[decoded(r) | {'result':parse_json(r['result'],limit=MAX_SNAPSHOT_BYTES) if r['result'] else None} for r in self.s.all('SELECT * FROM scope_returns WHERE project=? ORDER BY id',(project,))],
                    'scope_return_packets':[decoded(r) for r in self.s.all('SELECT * FROM scope_return_packets WHERE project=? ORDER BY proposal,level,ordinal,id',(project,))]}
            if self.s.one('SELECT 1 FROM subplans WHERE project=? LIMIT 1',(project,)):
                spec['subplan_history']={table:[decoded(r) for r in self.s.all(f'SELECT * FROM {table} WHERE project=? ORDER BY id',(project,))]
                                         for table in ('subplans','subplan_packets','subplan_compositions')}
            if has_local:
                spec['local_execution_history']={
                    'local_execution_proposals':[decoded(r) for r in self.s.all('SELECT * FROM local_execution_proposals WHERE project=? ORDER BY id',(project,))],
                    'local_execution_packets':[decoded(r) for r in self.s.all('SELECT * FROM local_execution_packets WHERE project=? ORDER BY proposal,ordinal',(project,))],
                    'local_execution_records':[decoded(r) for r in self.s.all('SELECT * FROM local_execution_records WHERE project=? ORDER BY proposal,created,id',(project,))],
                }
            if has_execution:
                spec['execution_control_history']=self._execution_control_history(project)
            # dev28 Unit A history is additive to the established v2-v4
            # specification shape.  The dedicated traceability archive is the
            # portable source-of-truth export; this section gives ordinary
            # read-only specification consumers a complete historical view
            # without turning it into runtime state or fresh evidence.
            trace_tables=('traceability_sets','traceability_revisions','traceability_items',
                          'traceability_proposals','traceability_decisions','traceability_mappings',
                          'traceability_bindings','traceability_records')
            if all(self.s.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)) for table in trace_tables):
                trace_rows={table:[decoded(row) for row in self.s.all(f'SELECT * FROM {table} WHERE project=? ORDER BY id',(project,))]
                            for table in trace_tables}
                trace_rows.update({'format':'daikibo.traceability-history.v10',
                                   'history_version':10,
                                   'runtime_restore_supported':False,
                                   'fresh_review_or_test_evidence':False,
                                   'historical_only':True})
                spec['traceability_history']=trace_rows
            if has_assurance:
                assurance_tables=('assurance_objects','assurance_events','assurance_heads','assurance_refs')
                assurance_queries={
                    'assurance_objects':'SELECT * FROM assurance_objects WHERE project=? ORDER BY id',
                    'assurance_events':'SELECT * FROM assurance_events WHERE project=? ORDER BY id',
                    'assurance_heads':'SELECT * FROM assurance_heads WHERE project=? ORDER BY logical_id',
                    'assurance_refs':'SELECT r.* FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE o.project=? ORDER BY r.object_id,r.ordinal',
                }
                spec['assurance_history']={table:self.s.all(assurance_queries[table],(project,)) for table in assurance_tables}
                spec['assurance_history'].update({'format':'daikibo.assurance-history.v1',
                                                   'history_version':1,
                                                   'runtime_restore_supported':False,
                                                   'fresh_review_or_test_evidence':False})
            if required_contracts:
                spec['assurance_history'].update(format='daikibo.assurance-history.v2', history_version=2, required_contracts=required_contracts)
            spec['external_reference_note']='Recorded review/receipt IDs are historical references, not newly verified evidence. Runtime recovery requires the separate full system backup.'
            if has_assurance:
                # The direct representation carries exactly the same finite
                # context/CAS contract as the chunked representation.  No
                # blob_put, event, key, or task mutation occurs here.
                spec['observed_context']=make_observed_context(self.s, project, spec)
            encoded_spec=canonical(spec)
            if len(encoded_spec) > MAX_SNAPSHOT_BYTES:
                raise Fault('snapshot_too_large', 'Specification export exceeds the explicit size limit', {
                    'kind': 'specification_bytes', 'required': len(encoded_spec),
                    'limit': MAX_SNAPSHOT_BYTES,
                })
            validate_specifications(spec)
        return spec

    @staticmethod
    def _snapshot(project,blob,content,extra_files=None):
        rid='spec-'+project
        snap={'format':'snapshot.v1','repos':{rid:{'name':'specifications','head':None,
              'files':extra_files or {'baseline.json':{'kind':'file','blob':blob,'size':len(content),'mode':0o100644}},
              'bytes':sum(f['size'] for f in (extra_files or {}).values()) if extra_files else len(content),'unknown':[]}}}
        snap['digest']=digest(snap)
        return rid,snap

    def create(self,actor,project,layout='auto',chunk_bytes=1024*1024):
        self.k.project(actor,project);actor.require('owner','agent',project=project)
        need(layout in {'auto','legacy','chunked'},'invalid_layout','Expected auto, legacy or chunked')
        with self.s.transaction():
            validate_origin_store(self.s)
            estimated = self.s.one('SELECT COALESCE(sum(characters),0)*4 AS n FROM sources WHERE project=?',(project,))['n']
            for table in ('artifacts','decisions','changes','conflicts','documents','programs','breakdowns','breakdown_packets'):
                estimated += 2*self.s.one(f'SELECT COALESCE(sum(length(CAST(body AS BLOB))),0) AS n FROM {table} WHERE project=?',(project,))['n']
            estimated += 2*self.s.one('SELECT COALESCE(sum(length(CAST(r.body AS BLOB))),0) AS n FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=?',(project,))['n']
            estimated += 2*self.s.one("SELECT COALESCE(sum(json_extract(body,'$.bytes')),0) AS n FROM documents WHERE project=?",(project,))['n']
            estimated += 2*self.s.one('SELECT COALESCE(sum(length(l.source)+length(l.target)+length(l.relation)+length(l.basis)+128),0) AS n FROM links l JOIN artifacts a ON a.id=l.source WHERE a.project=?',(project,))['n']
            origin_support = True
            estimated += 2*self.s.one('SELECT COALESCE(sum(length(CAST(body AS BLOB))),0) AS n FROM program_origins WHERE project=?',(project,))['n']
            staged = bool(self.s.one('SELECT 1 FROM breakdown_uploads WHERE project=? LIMIT 1',(project,)))
            partial = bool(self.s.one('SELECT 1 FROM subplans WHERE project=? LIMIT 1',(project,)))
            local_executions = bool(self.s.one('SELECT 1 FROM local_execution_proposals WHERE project=? LIMIT 1',(project,)))
            execution_controls = self._execution_control_history_available(project)
            collector_recovery = self._collector_failure_history_available(project)
            traceability_tables = ('traceability_sets','traceability_revisions','traceability_items',
                                   'traceability_proposals','traceability_decisions','traceability_mappings',
                                   'traceability_bindings','traceability_records')
            traceability = all(self.s.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
                               for table in traceability_tables) and bool(
                                   self.s.one('SELECT 1 FROM traceability_sets WHERE project=? LIMIT 1', (project,)))
            from .domain_responsibility import store_required_contracts
            domain_contracts = store_required_contracts(self.s, project)
            assurance = bool(domain_contracts) or bool(self.s.one('SELECT 1 FROM assurance_objects WHERE project=? LIMIT 1', (project,)))
            need(not(layout=='legacy' and partial),'legacy_cannot_preserve_subplans','Use auto/chunked to retain partial-plan history')
            need(not(layout=='legacy' and local_executions),'legacy_cannot_preserve_local_executions','Use auto or chunked to retain local execution history')
            need(not(layout=='legacy' and execution_controls),'legacy_cannot_preserve_execution_controls','Use auto or chunked to retain execution-control history')
            need(not(layout=='legacy' and collector_recovery),'legacy_cannot_preserve_collector_recovery','Use auto or chunked to retain collector-failure history')
            delegated = bool(self.s.one('SELECT 1 FROM workstreams WHERE project=? LIMIT 1',(project,)))
            need(not(layout=='legacy' and delegated),'legacy_cannot_preserve_workstreams','Use auto/chunked to retain delegation history')
            task_history = bool(self.s.one('SELECT 1 FROM task_revision_history WHERE project=? LIMIT 1',(project,)) or self.s.one('SELECT 1 FROM task_revision_proposals WHERE project=? LIMIT 1',(project,)))
            need(not(layout=='legacy' and task_history),'legacy_cannot_preserve_task_history','Use auto/chunked to retain task revision history')
            need(not(layout=='legacy' and traceability),'legacy_cannot_preserve_traceability','Use auto or chunked to retain traceability pins/staging')
            need(not(layout=='legacy' and assurance),'legacy_cannot_preserve_assurance','Use auto or chunked to retain immutable assurance history')
            # v1 has no staging tables. Never silently omit a new planning record
            # merely because the aggregate data set is small.
            need(not (layout=='legacy' and staged),'legacy_cannot_preserve_staging',
                 'Legacy export cannot retain staged plans; use auto or chunked')
            use_chunks = layout=='chunked' or (layout=='auto' and (partial or local_executions or execution_controls or collector_recovery or staged or task_history or delegated or traceability or assurance or estimated>16*1024*1024))
            body={'project':project,
                  'artifacts':self.s.all("SELECT id,kind,revision,digest FROM artifacts WHERE project=? AND status='accepted' ORDER BY id",(project,)),
                  'repos':self.s.all('SELECT id,name,head FROM repos WHERE project=? ORDER BY id',(project,))}
            ident,h=uid('BASE'),digest(body)
            if use_chunks:
                from . import archive_chunks
                payload=archive_chunks.build(self.s,project,ident,body,chunk_bytes,execution_controls=execution_controls,collector_recovery=collector_recovery,traceability=traceability,assurance=assurance,program_origins=origin_support)
                archive_chunks.validate(payload,self.s.blob_get)
            else:
                payload={'format':FORMAT,'baseline_id':ident,'baseline':body,
                         'specifications':self.export_current(actor,project),'generated_do_not_edit':True}
            content=canonical(payload)
            need(len(content)<=MAX_SNAPSHOT_BYTES,'snapshot_too_large','Specification archive exceeds the explicit size limit; no partial archive was accepted')
            blob=self.s.blob_put(content)
            files=archive_chunks.snapshot_files(payload,blob,self.s) if use_chunks else None
            rid,snapshot=self._snapshot(project,blob,content,files)
            self.s.execute('INSERT INTO baselines VALUES(?,?,?,?,?,?)',(ident,project,canonical(body).decode(),h,None,timestamp()))
            committed=Snapshots(self.s,self.sec,self.k).commit_snapshot(snapshot,rid,'Baseline '+ident,ref='refs/heads/current')
            bare=Path(committed['git_dir']);ref='refs/daikibo/baselines/'+ident
            git(bare,'update-ref',ref,committed['commit'],'0'*40)
            self.s.execute('UPDATE baselines SET git_commit=? WHERE id=?',(committed['commit'],ident))
            self.s.execute('INSERT INTO knowledge_snapshots VALUES(?,?,?,?,?)',(ident,project,blob,committed['commit'],timestamp()))
            self.sec.event(project,'baseline_created',actor.id,{'id':ident,'digest':h,'snapshot_blob':blob,'git_commit':committed['commit']})
        return {'id':ident,'digest':h,'body':body,'git_commit':committed['commit'],'snapshot_blob':blob,'self_contained_specifications':True,'layout':'chunked' if use_chunks else 'legacy'}

    def _load(self,actor,baseline):
        row=self.s.one('SELECT * FROM baselines WHERE id=?',(baseline,),True);self.k.project(actor,row['project'])
        saved=self.s.one('SELECT * FROM knowledge_snapshots WHERE baseline=?',(baseline,))
        need(saved,'legacy_baseline','Old baseline lacks the self-contained archive; create a new baseline rather than reconstructing historical content from current state')
        data=self.s.blob_get(saved['blob']);payload=parse_json(data,limit=MAX_SNAPSHOT_BYTES)
        from . import archive_chunks
        need(payload.get('format') in ({FORMAT} | archive_chunks.FORMATS) and payload.get('baseline_id')==baseline,'invalid_snapshot','Baseline identity/format differs')
        need(digest(payload['baseline'])==row['digest'] and payload['baseline']['project']==row['project'],'invalid_snapshot','Baseline metadata differs')
        counts=archive_chunks.validate(payload,self.s.blob_get) if payload['format'] in archive_chunks.FORMATS else validate_payload(payload)
        return row,saved,data,payload,counts

    def get(self,actor,baseline):
        row,saved,_,_,counts=self._load(actor,baseline)
        return {'baseline':baseline,'project':row['project'],'digest':row['digest'],
                'snapshot_blob':saved['blob'],'git_commit':saved['git_commit'],'counts':counts,
                'runtime_restore_supported':False,'revalidated_semantic_judgments':False}

    def list(self,actor,project,limit=100,offset=0):
        self.k.project(actor,project);number(limit,'limit',1,1000,integer=True);number(offset,'offset',0,10**12,integer=True)
        rows=self.s.all('''SELECT b.id,b.digest,b.git_commit,b.created,k.blob AS snapshot_blob
            FROM baselines b LEFT JOIN knowledge_snapshots k ON k.baseline=b.id WHERE b.project=?
            ORDER BY b.created,b.id LIMIT ? OFFSET ?''',(project,limit+1,offset))
        return {'baselines':rows[:limit],'next_offset':offset+limit if len(rows)>limit else None}

    def artifact_history(self,actor,artifact,limit=100,offset=0):
        current=self.k.artifact(actor,artifact);number(limit,'limit',1,1000,integer=True);number(offset,'offset',0,10**12,integer=True)
        rows=self.s.all('SELECT * FROM revisions WHERE artifact=? ORDER BY revision LIMIT ? OFFSET ?',(artifact,limit+1,offset))
        return {'artifact':artifact,'current_revision':current['revision'],'current_status':current['status'],
                'revisions':[decoded(r) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None,
                'note':'Revision rows preserve their creation-time status; current acceptance is recorded separately.'}

    def verify(self,actor,baseline):
        row,saved,data,payload,counts=self._load(actor,baseline);problems=[]
        bare=self.s.home/'git'/('spec-'+row['project'])
        if not bare.is_dir():problems.append('git_view_missing')
        else:
            shown=git(bare,'show',saved['git_commit']+':baseline.json',check=False)
            if shown.returncode:problems.append('git_object_missing')
            elif shown.stdout!=data:problems.append('git_content_differs')
            if payload.get('format') in {'daikibo.knowledge-snapshot.v2','daikibo.knowledge-snapshot.v3'}:
                for h,part in payload['objects'].items():
                    chunk=git(bare,'show',saved['git_commit']+':objects/'+h,check=False)
                    if chunk.returncode or len(chunk.stdout)!=part['bytes'] or digest(chunk.stdout)!=h:
                        problems.append('git_chunk_missing_or_changed:'+h)
            ref=git(bare,'rev-parse','--verify','refs/daikibo/baselines/'+baseline,check=False)
            if ref.returncode or ref.stdout.decode().strip()!=saved['git_commit']:problems.append('baseline_ref_differs')
        if row['git_commit']!=saved['git_commit']:problems.append('database_projection_pointer_differs')
        return {'baseline':baseline,'snapshot_verified':True,'git_view_verified':not problems,
                'problems':problems,'counts':counts,'semantic_acceptance_repeated':False}

    def rebuild_git(self,actor,baseline,expected_snapshot,reason):
        text(reason,'rebuild reason',4000)
        with self.s.transaction():
            row,saved,data,payload,_=self._load(actor,baseline);actor.require('owner','agent',project=row['project'])
            need(expected_snapshot==saved['blob'],'stale_snapshot','Explicit expected snapshot differs')
            check=self.verify(actor,baseline)
            if check['git_view_verified']:return {**check,'replayed':True,'git_commit':saved['git_commit']}
            from . import archive_chunks
            files=archive_chunks.snapshot_files(payload,saved['blob'],self.s) if payload['format'] in archive_chunks.FORMATS else None
            rid,snapshot=self._snapshot(row['project'],saved['blob'],data,files)
            result=Snapshots(self.s,self.sec,self.k).commit_snapshot(snapshot,rid,'Regenerate derived baseline '+baseline,ref='refs/heads/recovered/'+baseline)
            bare=Path(result['git_dir']);git(bare,'update-ref','refs/daikibo/baselines/'+baseline,result['commit'])
            latest=self.s.one('SELECT id FROM baselines WHERE project=? ORDER BY created DESC,id DESC LIMIT 1',(row['project'],),True)
            if latest['id']==baseline:git(bare,'update-ref','refs/heads/current',result['commit'])
            self.s.execute('UPDATE knowledge_snapshots SET git_commit=? WHERE baseline=?',(result['commit'],baseline))
            self.s.execute('UPDATE baselines SET git_commit=? WHERE id=?',(result['commit'],baseline))
            self.sec.event(row['project'],'baseline_git_regenerated',actor.id,{'baseline':baseline,'snapshot_blob':saved['blob'],
                           'previous_commit':saved['git_commit'],'generated_commit':result['commit'],'reason':reason,'canonical_specifications_changed':False})
        return {**self.verify(actor,baseline),'git_commit':result['commit'],'replayed':False}

    def export_archive(self,actor,baseline):
        row,saved,data,payload,counts=self._load(actor,baseline)
        destination=self.s.home/'exports'/('spec-'+baseline+'.zip');destination.parent.mkdir(exist_ok=True)
        from . import archive_chunks
        if payload['format'] in archive_chunks.FORMATS:
            h=archive_chunks.export_zip(self.s,payload,saved['blob'],destination)
            blob=self.s.blob_put_file(destination)
            need(h==blob,'integrity_error','Export changed during collection')
            self.sec.event(row['project'],'baseline_exported',actor.id,{'baseline':baseline,'archive_blob':blob,'format':archive_chunks.archive_format(payload)})
            return {'baseline':baseline,'path':str(destination),'sha256':blob,'blob':blob,'bytes':destination.stat().st_size,
                    'counts':counts,'format':archive_chunks.archive_format(payload),'warning':'Original user text and recorded history; not operational restore or new acceptance.'}
        manifest={'format':'daikibo.knowledge-archive.v1','baseline':baseline,'project':row['project'],
                  'files':{'snapshot.json':{'sha256':saved['blob'],'bytes':len(data)}},
                  'contains_runtime_credentials':False,'runtime_restore_supported':False}
        # Deterministic bytes; a repeat never silently exports a newer version.
        fd,temporary=tempfile.mkstemp(prefix='.spec-export-',suffix='.tmp',dir=destination.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                    for name,content in [('snapshot.json',data),('manifest.json',canonical(manifest))]:
                        info=zipfile.ZipInfo(name,(2026,9,11,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;archive.writestr(info,content)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        blob=self.s.blob_put(destination.read_bytes())
        self.sec.event(row['project'],'baseline_exported',actor.id,{'baseline':baseline,'archive_blob':blob})
        return {'baseline':baseline,'path':str(destination),'sha256':blob,'blob':blob,'bytes':destination.stat().st_size,
                'counts':counts,'warning':'Specification sources may contain confidential user text. This is not an operational backup or a new completion certificate.'}

    def inspect_archive(self,actor,path,expected_sha256):
        actor.require('owner','agent','observer')
        return inspect_archive(path,expected_sha256)


def inspect_archive(path,expected_sha256):
    """Verify a portable archive without a controller, repository, provider or original DB."""
    source=Path(path)
    from . import archive_chunks
    need(source.is_file(),'invalid_archive','Archive is absent')
    try:
        with zipfile.ZipFile(source) as probe:
            need(probe.getinfo('manifest.json').file_size<=65536,'invalid_archive','Manifest exceeds limit')
            header=parse_json(probe.read('manifest.json'),limit=65536)
        if header.get('format') in archive_chunks.ARCHIVE_FORMATS:
            return archive_chunks.inspect_zip(source,expected_sha256)
    except (KeyError,TypeError,ValueError,zipfile.BadZipFile) as exc:
        raise Fault('invalid_archive','Malformed archive header') from exc
    need(source.is_file() and source.stat().st_size<=MAX_SNAPSHOT_BYTES,'invalid_archive','Archive is absent or exceeds size bound')
    need(digest(source.read_bytes())==expected_sha256,'archive_mismatch','Archive digest differs')
    try:
        with zipfile.ZipFile(source) as archive:
            need(sorted(archive.namelist())==['manifest.json','snapshot.json'],'invalid_archive','Missing, duplicate or unexpected ZIP members')
            need(archive.getinfo('manifest.json').file_size<=65536 and archive.getinfo('snapshot.json').file_size<=MAX_SNAPSHOT_BYTES,'invalid_archive','Unexpected uncompressed size')
            manifest=parse_json(archive.read('manifest.json'),limit=65536)
            need(manifest['format']=='daikibo.knowledge-archive.v1' and set(manifest['files'])=={'snapshot.json'},'invalid_archive','Unsupported archive manifest')
            raw=archive.read('snapshot.json');record=manifest['files']['snapshot.json']
            need(len(raw)==record['bytes'] and digest(raw)==record['sha256'],'invalid_archive','Snapshot checksum differs')
            payload=parse_json(raw,limit=MAX_SNAPSHOT_BYTES)
            need(payload['format']==FORMAT and payload['baseline_id']==manifest['baseline'],'invalid_archive','Wrong baseline')
            need(payload['specifications']['project']==manifest['project'] and payload['baseline']['project']==manifest['project'],'invalid_archive','Project differs')
            inspection=validate_payload(payload, include_projection=True)
            counts=inspection['counts']
    except (KeyError,TypeError,ValueError,zipfile.BadZipFile,binascii.Error) as exc:
        raise Fault('invalid_archive','Malformed specification archive') from exc
    report={'verified':True,'baseline':manifest['baseline'],'project':manifest['project'],'snapshot_digest':record['sha256'],
            'counts':counts,'runtime_restore_supported':False,'new_test_or_review_evidence':False}
    report.update({key:inspection[key] for key in (
        'history_integrity','historical_reference_diagnostic_count',
        'historical_reference_diagnostics','historical_reference_diagnostics_total',
        'historical_reference_diagnostics_truncated','historical_reference_interpretation')
        if key in inspection})
    return report
