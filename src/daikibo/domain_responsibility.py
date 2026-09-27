"""Versioned, pure DOMAIN responsibility identity and closure contract."""
from dataclasses import dataclass

from .common import Fault, canonical, digest, need
from .assurance_additive import validate_artifact_structural_obligations
from .assurance_relations import validate_typed_ref

SCOPE_V2 = "assurance.scope.v2"
OBLIGATIONS_V2 = "assurance.obligations.v2"
NODE_V1 = "assurance.node-contract.v1"
NODE_V2 = "assurance.node-contract.v2"
DOMAIN_PACKET = "assurance.domain-node-review.v1"
DOMAIN_ROLE = "domain_responsibility"
DOMAIN_SELECTORS = {"domain": DOMAIN_ROLE, "accepted_domain": DOMAIN_ROLE}

@dataclass(frozen=True)
class ResponsibilityDiagnostic:
    """A retained-item diagnostic and the rejection used by complete issuers."""
    unresolved: dict
    error: Fault


def responsibility_material(ref, kind, body, *, resolver=None):
    """Return valid exact Q records, dependency refs, and retained-item diagnostics.

    Invalid canonical items do not erase valid siblings or supplemental material.
    Structural declarations retain their existing all-or-nothing schema validation.
    Callers issuing a complete contract must use responsibility_records instead.
    """
    ref = dict(validate_typed_ref(ref, project=ref["project"], expected_kinds={"artifact"}))
    ref.pop("identity_digest", None)
    values, diagnostics = [], []
    if kind == "domain":
        responsibilities = body.get("responsibilities")
        if type(responsibilities) is not list:
            diagnostics.append(ResponsibilityDiagnostic(
                {"code": "artifact_responsibilities_unavailable",
                 "reason": "Domain responsibility array is missing from retained artifact body",
                 "artifact": ref["artifact"], "status": "legacy_unavailable"},
                Fault("invalid_scope", "DOMAIN responsibilities must be explicit")))
        else:
            for i, value in enumerate(responsibilities):
                if type(value) is not str or not value.strip() or "\x00" in value:
                    diagnostics.append(ResponsibilityDiagnostic(
                        {"code": "artifact_responsibility_invalid",
                         "reason": "Domain responsibility is not a nonempty string",
                         "artifact": ref["artifact"], "index": i},
                        Fault("invalid_scope", "DOMAIN responsibility must be a nonempty string")))
                    continue
                values.append(("artifact_responsibility", f"/responsibilities/{i}", value))
    dependencies = {}
    if "structural_obligations" in body:
        try:
            structural = validate_artifact_structural_obligations(
                body["structural_obligations"], kind=kind, project=ref["project"], resolver=resolver)
        except Fault as exc:
            diagnostics.append(ResponsibilityDiagnostic(
                {"code": "artifact_structural_invalid",
                 "reason": "Stored artifact structural_obligations is invalid and cannot be verified",
                 "artifact": ref["artifact"],
                 "detail": "explicit_null" if body["structural_obligations"] is None else exc.message}, exc))
        else:
            for i, value in enumerate(structural["items"]):
                values.append(("artifact_structural_responsibility",
                               f"/structural_obligations/responsibilities/{i}", value))
                if value["type"] == "domain_reference":
                    dependencies[canonical(value["domain"])] = value["domain"]
    records = []
    for category, pointer, value in values:
        identity = {"category": category, "source_ref": ref, "pointer": pointer,
                    "value_digest": digest(value)}
        records.append({"id": "obligation:" + digest(identity), **identity,
                        "contributors": [], "introduced_at": "plan", "required_at": "plan"})
    return (sorted(records, key=lambda x: x["id"]),
            [dependencies[k] for k in sorted(dependencies)], diagnostics)


def responsibility_records(ref, kind, body, *, resolver=None):
    """Return complete Q records and dependencies, rejecting every diagnostic."""
    records, dependencies, diagnostics = responsibility_material(ref, kind, body, resolver=resolver)
    if diagnostics:
        raise diagnostics[0].error
    return records, dependencies

def validate_v2_body(kind, body):
    if kind == "scope":
        need(set(body) == {"format", "project", "roots", "selection_rules", "exclusion_proposals",
                           "authority_refs", "discovery_unknowns"}, "invalid_scope", "Scope v2 keys differ")
        need(body["format"] == SCOPE_V2, "invalid_scope", "Scope v2 format differs")
        roots = body["roots"]
        need(type(roots) is list and roots == sorted(roots, key=canonical)
             and len({canonical(x) for x in roots}) == len(roots), "invalid_scope", "Scope roots must be canonical and unique")
        for ref in roots:
            validate_typed_ref(ref, project=body["project"], expected_kinds={"artifact", "source", "population"})
    else:
        need(set(body) == {"format", "project", "scope_ref", "derivation_version", "input_refs",
                           "enumeration_status", "obligations"}, "invalid_scope", "Obligations v2 keys differ")
        need(body["format"] == OBLIGATIONS_V2 and body["derivation_version"] == "assurance-obligations-v2"
             and body["enumeration_status"] == "complete", "invalid_scope", "Obligations v2 contract differs")
        records = body["obligations"]
        need(type(records) is list and all(type(x) is dict and type(x.get("id")) is str for x in records),
             "invalid_scope", "Obligation records are malformed")
        need(records == sorted(records, key=lambda x:x["id"]) and len({x["id"] for x in records}) == len(records),
             "invalid_scope", "Obligation identities must be unique and canonical")
        for q in records:
            need(q.get("kind") != "empty_scope", "invalid_scope", "v2 has no empty placeholder")
            if q.get("category") in {"artifact_responsibility", "artifact_structural_responsibility"}:
                identity = {k:q.get(k) for k in ("category", "source_ref", "pointer", "value_digest")}
                need(q == {"id":"obligation:"+digest(identity), **identity, "contributors":[],
                           "introduced_at":"plan", "required_at":"plan"}, "invalid_scope", "Responsibility identity differs")
        refs = body["input_refs"]
        need(type(refs) is list and refs == sorted(refs, key=canonical) and len({canonical(x) for x in refs}) == len(refs),
             "invalid_scope", "Dependency closure must be canonical and unique")
        for ref in refs:
            validate_typed_ref(ref, project=body["project"])


def required_contracts(objects, runs=()):
    from .common import parse_json
    contracts = set()
    for row in objects:
        body = row.get("body", {})
        if type(body) is str: body = parse_json(body)
        fmt = body.get("format")
        if fmt == SCOPE_V2: contracts.add(SCOPE_V2)
        if fmt == OBLIGATIONS_V2: contracts.update({SCOPE_V2, OBLIGATIONS_V2})
        if fmt == "assurance.profile.v5":
            contracts.update({SCOPE_V2, OBLIGATIONS_V2, "assurance.profile.v5", NODE_V2})
    for row in runs:
        if row.get("role") == DOMAIN_ROLE: contracts.update({NODE_V2, DOMAIN_PACKET})
    return sorted(contracts)


def store_required_contracts(store, project):
    return required_contracts(store.all("SELECT body FROM assurance_objects WHERE project=?", (project,)),
        store.all("SELECT role FROM runs WHERE project=?", (project,)) +
        store.all("SELECT role FROM receipts WHERE project=?", (project,)))


def validate_contract_manifest(declared, expected, *, modern, code):
    if modern:
        need(type(declared) is list and declared == expected and bool(expected), code,
             "Required contracts differ from the retained closure")
    else:
        need(not expected and declared is None, code, "New DOMAIN contract requires a versioned export")


def validate_domain_history(context, project, artifact_resolver, source_resolver, blob_get):
    """Validate saved DOMAIN prompt/receipt closure using retained identities only."""
    from .common import parse_json
    def body(row):
        value = row.get('body', {})
        return parse_json(value) if type(value) is str else value
    runs = {x['id']:x for x in context.get('runs', [])}
    for row in runs.values():
        if row.get('role') != DOMAIN_ROLE: continue
        run = body(row)
        prompt_hash = run.get('input_digest')
        raw = blob_get(prompt_hash)
        need(isinstance(raw, bytes) and digest(raw)==prompt_hash, 'invalid_archive', 'DOMAIN prompt CAS differs')
        prompt = parse_json(raw)
        material = prompt.get('context', {}).get('domain_review')
        need(type(material) is dict and set(material)=={'format','node_contract','node_ref','artifact','sources',
              'accepted_invariants','responsibility_obligations','dependency_refs','required_coverage'},
             'invalid_archive','DOMAIN saved material shape differs')
        need(material['format']==DOMAIN_PACKET and material['node_contract']==NODE_V2,
             'invalid_archive','DOMAIN saved contract differs')
        ref = material['node_ref']
        validate_typed_ref(ref, project=project, expected_kinds={'artifact'})
        artifact = artifact_resolver(ref)
        expected_artifact = {k:artifact[k] for k in ('id','project','kind','revision','digest','body')}
        need(artifact['kind']=='domain' and material['artifact']==expected_artifact,
             'invalid_archive','DOMAIN retained revision material differs')
        records, deps = responsibility_records(ref,'domain',artifact['body'],resolver=artifact_resolver)
        deps,source_rows=artifact_dependency_closure(ref,artifact_resolver,source_resolver)
        sources=[]
        for source in source_rows:
            ident=source['id']
            need(source is not None and source.get('project')==project,'invalid_archive','DOMAIN source missing')
            raw_source = blob_get(source['blob'])
            need(isinstance(raw_source,bytes) and digest(raw_source)==source['blob'],'invalid_archive','DOMAIN source CAS differs')
            sources.append({'id':ident,'digest':source['blob'],'content':raw_source.decode('utf-8')})
        fields=['responsibilities','non_responsibilities','owned_data','interfaces']
        if 'structural_obligations' in artifact['body']: fields.append('structural_obligations')
        markers=sorted([q['id'] for q in records]+['domain-field:'+digest({'source_ref':ref,'pointer':'/'+field,
            'value_digest':digest(artifact['body'][field])}) for field in fields])
        need(material['responsibility_obligations']==records and material['sources']==sources and
             material['dependency_refs']==sorted(deps,key=canonical) and material['required_coverage']==markers and
             prompt.get('context',{}).get('required_coverage')==markers,
             'invalid_archive','DOMAIN semantic population, sources or marker closure differs')
        need(prompt.get('subject')==artifact['id'] and prompt.get('binding')==artifact['digest'] and
             prompt.get('role')==DOMAIN_ROLE and row.get('project')==project,
             'invalid_archive','DOMAIN prompt identity differs')
        for invariant in material['accepted_invariants']:
            iref={'kind':'artifact','project':project,'artifact':invariant['id'],'revision':invariant['revision'],
                  'body_digest':invariant['digest']}
            retained=artifact_resolver(iref)
            need(invariant=={'id':retained['id'],'revision':retained['revision'],'digest':retained['digest'],
                 'statement':retained['body'].get('statement'),'constraints':retained['body'].get('constraints',{})},
                 'invalid_archive','DOMAIN retained invariant differs')
        for receipt_row in context.get('receipts',[]):
            if receipt_row.get('run') != row['id']: continue
            receipt=body(receipt_row)
            need(receipt_row.get('role')==DOMAIN_ROLE and receipt.get('role')==DOMAIN_ROLE and
                 receipt.get('subject')==artifact['id'] and receipt.get('binding')==artifact['digest'] and
                 receipt.get('input_digest')==prompt_hash,
                 'invalid_archive','DOMAIN receipt identity differs from saved prompt')


def validate_domain_store(store):
    """Read-only closure preflight for a private restored candidate home."""
    from .assurance import validate_assurance_rows
    from .knowledge_history import decoded
    from .portable_context import project_context_rows
    for project_row in store.all('SELECT id FROM projects'):
        project=project_row['id']
        if not store_required_contracts(store,project): continue
        tables={
            'assurance_objects':store.all('SELECT * FROM assurance_objects WHERE project=?',(project,)),
            'assurance_events':store.all('SELECT * FROM assurance_events WHERE project=?',(project,)),
            'assurance_heads':store.all('SELECT * FROM assurance_heads WHERE project=?',(project,)),
            'assurance_refs':store.all('SELECT r.* FROM assurance_refs r JOIN assurance_objects o ON o.id=r.object_id WHERE o.project=?',(project,)),
        }
        context=project_context_rows(store,project)
        for table in ('artifacts','sources','task_revision_history','traceability_items','traceability_sets','traceability_revisions','traceability_records'):
            context[table]=store.all(f'SELECT * FROM {table} WHERE project=?',(project,))
        context['revisions']=store.all('SELECT r.* FROM revisions r JOIN artifacts a ON a.id=r.artifact WHERE a.project=?',(project,))
        context['assurance_objects']=tables['assurance_objects']
        context={table:[decoded(x) for x in rows] for table,rows in context.items()}
        indexes={table:{(canonical([x['artifact'],x['revision']]).decode() if table=='revisions' else x['id']):x for x in rows}
                 for table,rows in context.items()}
        def external(table,key):return indexes.get(table,{}).get(key)
        external.context_rows=context
        validate_assurance_rows(tables,project,external,store.blob_get)


def artifact_dependency_closure(root_ref, resolve_artifact, resolve_source):
    """Exact structural artifact/source transitive closure, independent of row order."""
    root_key=canonical(root_ref);seen=set();dependencies={};sources={}
    def visit(ref):
        key=canonical(ref)
        if key in seen:return
        seen.add(key)
        artifact=resolve_artifact(ref)
        _,children=responsibility_records(ref,artifact['kind'],artifact['body'],resolver=resolve_artifact)
        for ident in artifact['body'].get('source_refs',[]):
            source=resolve_source(ident)
            need(isinstance(source,dict) and source.get('project')==ref['project'],
                 'unresolved_reference','Responsibility source is missing or foreign',ident)
            source_ref={'kind':'source','project':ref['project'],'source':ident,'blob_digest':source['blob']}
            validate_typed_ref(source_ref,project=ref['project'])
            dependencies[canonical(source_ref)]=source_ref;sources[ident]=source
        for child in children:
            if canonical(child)!=root_key:dependencies[canonical(child)]=child
            visit(child)
    visit(root_ref)
    return [dependencies[x] for x in sorted(dependencies)], [sources[x] for x in sorted(sources)]
