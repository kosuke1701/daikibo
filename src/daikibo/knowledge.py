"""D02 — sources, immutable specifications, typed trace graph and baselines."""
from __future__ import annotations
import base64
from pathlib import Path
from .common import (Actor, Fault, DEFAULT_STRING_ITEM_MAXIMUM, canonical, digest, need, obj,
                     parse_json, strings, text, timestamp, uid)
from .contracts import FORMAT as ARTIFACT_TYPED_CONTRACT_FORMAT
from .assurance_additive import (
    ARTIFACT_STRUCTURAL_KINDS,
    STRUCTURAL_MAX_ITEMS,
    STRUCTURAL_MAX_STATEMENT,
    structural_contract,
    validate_artifact_structural_obligations,
)

KINDS = {"outcome", "requirement", "acceptance", "domain", "design", "interface", "component", "test", "risk", "assumption", "decision", "scenario", "finding", "unknown", "glossary"}
RELATIONS = {"decomposes", "realizes", "verifies", "depends_on", "owns", "consumes", "supersedes", "derived_from"}

# Keep the public body contract next to the validator's source-of-truth values.
# The metadata is descriptive; validation below remains the authority and still
# accepts semantic fields that are not listed here.
ARTIFACT_BASE_REQUIRED_FIELDS = ("title", "statement")
ARTIFACT_KIND_REQUIRED_FIELDS = {
    "requirement": ("acceptance",),
    "domain": ("responsibilities", "non_responsibilities", "owned_data", "interfaces"),
    "interface": ("input", "output", "authentication", "errors", "idempotency", "compatibility", "consumers", "verification"),
}
ARTIFACT_TEXT_LIMITS = {"title": 400, "statement": 200000}
ARTIFACT_ARRAY_LIMITS = {
    "acceptance": {"maximum": 200, "nonempty": True},
    "responsibilities": {"maximum": 10000, "nonempty": False},
    "non_responsibilities": {"maximum": 10000, "nonempty": False},
    "owned_data": {"maximum": 10000, "nonempty": False},
    "interfaces": {"maximum": 10000, "nonempty": False},
}
ARTIFACT_ARRAY_ITEM_LIMIT = DEFAULT_STRING_ITEM_MAXIMUM
ARTIFACT_SOURCE_REF_LIMITS = {"maximum": 10000, "nonempty": True}
ARTIFACT_CONSTRAINT_SCALARS = ("string", "integer", "boolean", "number", "null")


def _artifact_required_fields(kind):
    return ARTIFACT_BASE_REQUIRED_FIELDS + ARTIFACT_KIND_REQUIRED_FIELDS.get(kind, ())


def _artifact_field_metadata(kind):
    fields = {
        "title": {"type": "string", "nonempty": True, "non_whitespace": True, "no_nul": True, "max_length": ARTIFACT_TEXT_LIMITS["title"]},
        "statement": {"type": "string", "nonempty": True, "non_whitespace": True, "no_nul": True, "max_length": ARTIFACT_TEXT_LIMITS["statement"]},
    }
    for field, limits in ARTIFACT_ARRAY_LIMITS.items():
        if field in _artifact_required_fields(kind):
            fields[field] = {
                "type": "array",
                "nonempty": limits["nonempty"],
                "max_items": limits["maximum"],
                "unique_items": True,
                "items": {"type": "string", "nonempty": True, "non_whitespace": True, "no_nul": True, "max_length": ARTIFACT_ARRAY_ITEM_LIMIT},
            }
    if kind == "requirement":
        fields["source_refs"] = {
            "type": "array",
            "required": False,
            "nonempty": ARTIFACT_SOURCE_REF_LIMITS["nonempty"],
            "max_items": ARTIFACT_SOURCE_REF_LIMITS["maximum"],
            "unique_items": True,
            "items": {"type": "string", "nonempty": True, "non_whitespace": True, "no_nul": True, "max_length": ARTIFACT_ARRAY_ITEM_LIMIT},
        }
    if kind == "interface":
        for field in ARTIFACT_KIND_REQUIRED_FIELDS["interface"]:
            fields[field] = {"required": True, "validation": "presence_only"}
    if kind in ARTIFACT_STRUCTURAL_KINDS:
        fields["structural_obligations"] = {
            "required": False,
            "type": "object",
            "format": "daikibo.structural-obligations.v1",
            "additional_properties": False,
            "responsibilities": {
                "type": "array", "max_items": STRUCTURAL_MAX_ITEMS,
                "item_types": ["statement", "domain_reference"],
                "statement_max_length": STRUCTURAL_MAX_STATEMENT,
            },
            "missing": "legacy_unavailable", "empty": "explicit_empty", "null": "invalid_input",
        }
    return fields


def _artifact_limits_metadata(kind):
    limits = {
        "title": {"max_length": ARTIFACT_TEXT_LIMITS["title"]},
        "statement": {"max_length": ARTIFACT_TEXT_LIMITS["statement"]},
    }
    for field, values in ARTIFACT_ARRAY_LIMITS.items():
        if field in _artifact_required_fields(kind):
            limits[field] = {"max_items": values["maximum"], "item_max_length": ARTIFACT_ARRAY_ITEM_LIMIT}
    if kind == "requirement":
        limits["source_refs"] = {"max_items": ARTIFACT_SOURCE_REF_LIMITS["maximum"], "item_max_length": ARTIFACT_ARRAY_ITEM_LIMIT}
    if kind in ARTIFACT_STRUCTURAL_KINDS:
        limits["structural_obligations"] = {"max_items": STRUCTURAL_MAX_ITEMS,
                                              "statement_max_length": STRUCTURAL_MAX_STATEMENT}
    return limits


def _artifact_body_contract():
    contracts = {}
    for kind in sorted(KINDS):
        contracts[kind] = {
            "type": "object",
            "required": list(_artifact_required_fields(kind)),
            "fields": _artifact_field_metadata(kind),
            "limits": _artifact_limits_metadata(kind),
            "json": {"finite": True},
            "constraints": {
                "optional": True,
                "type": "object",
                "flat": True,
                "key_type": "string",
                "value_types": list(ARTIFACT_CONSTRAINT_SCALARS),
            },
            "extra_fields": "allowed",
        }
        if kind == "interface":
            contracts[kind]["conditional"] = [
                {
                    "when": {"field": side, "type": "object", "format": ARTIFACT_TYPED_CONTRACT_FORMAT},
                    "required_keys": ["format", "schema"],
                    "allowed_keys": ["format", "schema"],
                    "schema_validation": "existing daikibo.type.v1 validator",
                    "validation_scope": "conditional",
                }
                for side in ("input", "output")
            ]
    return {"version": 2, "kinds": contracts,
            "additive": structural_contract()["artifact"]}


ARTIFACT_BODY_CONTRACT = _artifact_body_contract()


def artifact_body_contract():
    """Return a detached description of the existing artifact body validator."""
    return parse_json(canonical(ARTIFACT_BODY_CONTRACT))

def decode(row):
    if row and "body" in row:
        row = dict(row); row["body"] = parse_json(row["body"])
    return row

class Knowledge:
    def __init__(self, store, security):
        self.s, self.sec = store, security
        self.assessments = None

    def project(self, actor: Actor, project: str):
        actor.require("owner", "agent", "worker", "reviewer", "observer", project=project, task=actor.task)
        return self.s.one("SELECT * FROM projects WHERE id=?", (project,), True)

    def create_project(self, actor: Actor, name: str, config=None):
        actor.require("owner", "agent")
        need(actor.project is None, "forbidden", "Scoped capability cannot create projects")
        text(name, "project name", 200)
        config = config or {}
        obj(config, optional=("max_parallel", "budget_seconds", "max_attempts", "unknown_policy"))
        project = uid("PRJ")
        with self.s.transaction():
            self.s.execute("INSERT INTO projects(id,name,config,created) VALUES(?,?,?,?)", (project, name, canonical(config).decode(), timestamp()))
            self.sec.event(project, "project_created", actor.id, {"name": name})
        return {"id": project, "name": name, "stage": "discovery"}

    def source(self, actor: Actor, project: str, content: str, locator: str = "conversation"):
        self.project(actor, project)
        actor.require("owner", "agent", "worker", project=project, task=actor.task)
        text(content, "source", 8_000_000)
        text(locator, "locator", 4096)
        source = uid("SRC")
        blob = self.s.blob_put(content.encode())
        trust = "human" if actor.role == "owner" else "agent"
        with self.s.transaction():
            self.s.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?)", (source, project, blob, locator, len(content), actor.id, trust, timestamp()))
            self.sec.event(project, "source_registered", actor.id, {"source": source, "digest": blob, "trust": trust})
        return {"id": source, "digest": blob, "characters": len(content), "trust": trust}

    def source_read(self, actor, source: str, start=0, limit=12000):
        row = self.s.one("SELECT * FROM sources WHERE id=?", (source,), True)
        self.project(actor, row['project'])
        need(type(start) is int and start >= 0 and type(limit) is int and 1 <= limit <= 100000, "invalid_range", "Invalid source range")
        content = self.s.blob_get(row['blob']).decode()
        end = min(start + limit, len(content))
        return {"source": source, "digest": row['blob'], "start": start, "end": end,
                "content": content[start:end], "next_start": end if end < len(content) else None, "trust": row['trust']}

    def human_quote(self, actor, project, source, quote, after=0):
        """Bind a response to an existing human source without reclassifying it."""
        actor.require('owner', project=project)
        row=self.s.one('SELECT * FROM sources WHERE id=?',(source,),True)
        need(row['project']==project,'cross_project','Source belongs elsewhere')
        need(row['trust']=='human','human_input_required','Response needs a human source')
        need(row['created']>=after,'stale_user_input','Answer predates the displayed proposal or notice')
        content=self.s.blob_get(row['blob']).decode()
        text(quote,'exact user quotation',100000)
        start=content.find(quote)
        need(start>=0,'quote_mismatch','Quote must appear verbatim in the source')
        return {'source':source,'source_digest':row['blob'],'start':start,'end':start+len(quote),'quote':quote}

    def classify(self, actor, source: str, start: int, end: int, category: str, refs: list[str], reason: str):
        row = self.s.one("SELECT * FROM sources WHERE id=?", (source,), True)
        actor.require("owner", "agent", project=row['project'])
        need(type(start) is int and type(end) is int and 0 <= start < end <= row['characters'], "invalid_range", "Source classification range is invalid")
        need(category in {"requirement", "constraint", "question", "reference", "out_of_scope"}, "invalid_category", "Unknown disposition")
        strings(refs); text(reason, "classification reason", 12000)
        if category in {"requirement", "constraint"}:
            need(refs, "missing_reference", "Requirement and constraint classifications need artifact references")
        for ref in refs:
            need(self.artifact(actor, ref)['project'] == row['project'], "cross_project", "Reference belongs elsewhere")
        with self.s.transaction():
            overlaps = self.s.all("SELECT id FROM dispositions WHERE source=? AND start<? AND end>?", (source, end, start))
            need(not overlaps, "overlap", "This source region already has a disposition", overlaps)
            ident = uid("DISP")
            self.s.execute("INSERT INTO dispositions VALUES(?,?,?,?,?,?,?,?)", (ident, source, start, end, category, canonical(refs).decode(), reason, actor.id))
            self.sec.event(row['project'], "source_classified", actor.id, {"source": source, "range": [start, end], "category": category})
        return {"id": ident}

    def source_coverage(self, actor, project: str):
        self.project(actor, project)
        result = []
        for source in self.s.all("SELECT id,characters,blob FROM sources WHERE project=?", (project,)):
            gaps, cursor = [], 0
            for interval in self.s.all("SELECT start,end FROM dispositions WHERE source=? ORDER BY start", (source['id'],)):
                if interval['start'] > cursor:
                    gaps.append([cursor, interval['start']])
                cursor = max(cursor, interval['end'])
            if cursor < source['characters']:
                gaps.append([cursor, source['characters']])
            result.append({"source": source['id'], "digest": source['blob'], "unclassified": gaps})
        return {"sources": result, "structurally_complete": bool(result) and all(not x['unclassified'] for x in result) and not self.s.one("SELECT id FROM documents WHERE project=? AND status='needs_extraction'",(project,)),
                "meaning_review_required": True}

    @staticmethod
    def validate_body(kind, body):
        need(kind in KINDS, "invalid_kind", "Unknown artifact kind")
        need(isinstance(body, dict), "invalid_body", "Artifact body must be an object")
        text(body.get('title'), "title", ARTIFACT_TEXT_LIMITS["title"])
        text(body.get('statement'), "statement", ARTIFACT_TEXT_LIMITS["statement"])
        canonical(body)  # Reject NaN and non-JSON objects.
        if kind == "requirement":
            criteria = body.get('acceptance', [])
            limits = ARTIFACT_ARRAY_LIMITS["acceptance"]
            strings(criteria, "acceptance", maximum=limits["maximum"], nonempty=limits["nonempty"], item_maximum=ARTIFACT_ARRAY_ITEM_LIMIT)
            if "source_refs" in body:
                strings(body["source_refs"], "source_refs", maximum=ARTIFACT_SOURCE_REF_LIMITS["maximum"],
                       nonempty=ARTIFACT_SOURCE_REF_LIMITS["nonempty"], item_maximum=ARTIFACT_ARRAY_ITEM_LIMIT)
        if kind == "domain":
            for key in ARTIFACT_KIND_REQUIRED_FIELDS["domain"]:
                limits = ARTIFACT_ARRAY_LIMITS[key]
                strings(body.get(key), key, maximum=limits["maximum"], nonempty=limits["nonempty"], item_maximum=ARTIFACT_ARRAY_ITEM_LIMIT)
        if kind == "interface":
            for key in ARTIFACT_KIND_REQUIRED_FIELDS["interface"]:
                need(key in body, "incomplete_contract", f"Interface is missing {key}")
            from .contracts import validate_type
            for side in ('input','output'):
                value=body[side]
                if isinstance(value,dict) and value.get('format')==ARTIFACT_TYPED_CONTRACT_FORMAT:
                    need(set(value)=={'format','schema'},'invalid_contract','Typed contract needs format and schema only')
                    validate_type(value['schema'])
        if "structural_obligations" in body:
            validate_artifact_structural_obligations(body["structural_obligations"], kind=kind)
        if 'constraints' in body:
            need(isinstance(body['constraints'], dict), "invalid_constraints", "Constraints must be a flat map of explicit invariant keys to JSON values")
            need(all(isinstance(k, str) and type(v) in (str, int, bool, float, type(None)) for k,v in body['constraints'].items()),
                 "invalid_constraints", "Only scalar equality invariants are machine compared; semantic constraints require review")

    def propose(self, actor, project: str, kind: str, body: dict, owner: str = "unassigned", artifact_id: str | None = None):
        self.project(actor, project)
        actor.require("owner", "agent", project=project)
        self.validate_body(kind, body); text(owner, "owner", 300)
        if "structural_obligations" in body:
            self._validate_structural_references(actor, project, kind, body)
        from .standard_contracts import verify_projection
        verify_projection(self.s, project, body)
        ident = artifact_id or uid(kind.upper())
        text(ident, "artifact id", 200)
        h, now = digest(body), timestamp()
        with self.s.transaction():
            need(not self.s.one("SELECT id FROM artifacts WHERE id=?", (ident,)), "duplicate_id", "Artifact ID already exists")
            self.s.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)", (ident, project, kind, 1, "draft", canonical(body).decode(), h, owner, now))
            self.s.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)", (ident, 1, canonical(body).decode(), h, "draft", "initial proposal", actor.id, now))
            self.sec.event(project, "artifact_proposed", actor.id, {"id": ident, "kind": kind, "revision": 1, "digest": h})
        return self.artifact(actor, ident)

    def artifact(self, actor, artifact: str, revision: int | None = None):
        row = self.s.one("SELECT * FROM artifacts WHERE id=?", (artifact,), True)
        self.project(actor, row['project'])
        if revision is not None:
            historical = self.s.one("SELECT * FROM revisions WHERE artifact=? AND revision=?", (artifact, revision), True)
            row.update(historical)
        result=decode(row)
        from .standard_contracts import verify_projection
        verify_projection(self.s, result["project"], result["body"])
        return result

    def list_artifacts(self, actor, project: str, kind=None, limit=100, offset=0):
        self.project(actor, project)
        need(type(limit) is int and 1 <= limit <= 1000 and type(offset) is int and offset >= 0, "invalid_range", "Invalid list range")
        rows = self.s.all("SELECT * FROM artifacts WHERE project=? AND (? IS NULL OR kind=?) ORDER BY id LIMIT ? OFFSET ?", (project,kind,kind,limit,offset))
        return {"items": [decode(r) for r in rows], "next_offset": offset+len(rows) if len(rows)==limit else None}

    def revise(self, actor, artifact: str, expected_revision: int, body: dict, reason: str):
        row = self.artifact(actor, artifact)
        actor.require("owner", "agent", project=row['project'])
        need(row['status'] == 'draft', "change_required", "Accepted artifacts must be changed through the change workflow")
        return self._revise(actor, row, expected_revision, body, reason, "draft")

    def save(self, actor, artifact: str, expected_revision: int, body: dict, reason: str):
        """Save a draft, replaying only an identical save by the same actor.

        revise remains available for an intentional new revision. Adoption,
        accepted changes and a changed reason never take this replay path.
        """
        with self.s.transaction():
            row = self.artifact(actor, artifact)
            actor.require('owner', 'agent', project=row['project'])
            need(row['revision'] == expected_revision, 'stale_revision', 'Artifact changed concurrently')
            need(row['status'] == 'draft', 'change_required', 'Accepted artifacts require the change workflow')
            text(reason, 'revision reason', 12000)
            previous = self.s.one('SELECT reason,actor FROM revisions WHERE artifact=? AND revision=?',
                                  (artifact, expected_revision), True)
            if canonical(body) == canonical(row['body']) and reason == previous['reason'] and actor.id == previous['actor']:
                self.sec.event(row['project'], 'artifact_save_replayed', actor.id,
                               {'id': artifact, 'revision': expected_revision, 'digest': row['digest']})
                return {**row, 'unchanged': True}
            return self._revise(actor, row, expected_revision, body, reason, 'draft')

    def _revise(self, actor, row, expected_revision, body, reason, status):
        self.validate_body(row['kind'], body); text(reason, "revision reason", 12000)
        if "structural_obligations" in body:
            self._validate_structural_references(actor, row['project'], row['kind'], body)
        from .standard_contracts import verify_projection
        verify_projection(self.s, row["project"], body)
        h, revision = digest(body), expected_revision + 1
        with self.s.transaction():
            cursor = self.s.execute("UPDATE artifacts SET revision=?,body=?,digest=?,status=? WHERE id=? AND revision=?",
                                    (revision,canonical(body).decode(),h,status,row['id'],expected_revision))
            need(cursor.rowcount == 1, "stale_revision", "Artifact changed concurrently")
            self.s.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)", (row['id'],revision,canonical(body).decode(),h,status,reason,actor.id,timestamp()))
            impacted = self.invalidate(row['project'], [row['id']], reason)
            self.sec.event(row['project'], "artifact_revised", actor.id, {"id": row['id'], "revision": revision, "digest": h, "impacted": impacted})
        return self.artifact(actor, row['id'])

    def _validate_structural_references(self, actor, project, kind, body):
        """Resolve A's domain pins against the exact retained revision body."""
        def resolve(ref):
            resolved = self.artifact(actor, ref["artifact"], ref["revision"])
            need(resolved["project"] == project, "cross_project", "Domain reference belongs to another project")
            need(resolved["digest"] == ref["body_digest"], "stale_reference", "Domain reference body digest differs")
            return resolved
        validate_artifact_structural_obligations(
            body["structural_obligations"], kind=kind, project=project, resolver=resolve,
        )

    def accept(self, actor, artifact: str, expected_revision: int, review_receipt: str | None = None):
        initial = self.artifact(actor, artifact)
        actor.require("owner", "agent", project=initial['project'])
        with self.s.transaction():
            row = self.artifact(actor, artifact)
            actor.require("owner", "agent", project=row['project'])
            need(row['revision'] == expected_revision, "stale_revision", "Review the current proposal")
            need(row['status'] == 'draft', "invalid_state", "Only a draft can be accepted")
            if actor.role != 'owner':
                need(self.assessments is not None and review_receipt, "review_required", "Managed consistency assessment required")
                review = self.assessments.receipt(review_receipt)
                role = review.get('role')
                roles = {'requirements', 'design', 'consistency'}
                need(role in roles, 'stale_evidence', 'Artifact approval has the wrong review role')
                materials = getattr(self.assessments, 'review_materials', None)
                need(materials is not None and callable(getattr(materials, 'artifact', None)),
                     'review_material_unavailable', 'Current artifact review material is unavailable')
                material = materials.artifact(actor, artifact, role)
                self.assessments.require_review(review_receipt, artifact, material['binding'],
                                                {role}, latest=True)
                # A source-grounded proposal may be adopted without asking about technical detail.
                sources = row['body'].get('source_refs', [])
                need(sources, "ungrounded_requirement", "Autonomous adoption must cite trusted source material")
                for source in sources:
                    s = self.s.one("SELECT project,trust FROM sources WHERE id=?", (source,), True)
                    need(s['project']==row['project'] and s['trust']=='human', "human_input_required", "Source is not authenticated human input")
            conflicts = self.explicit_conflicts(row['project'], row['body'], exclude=[artifact])
            need(not conflicts, "conflict", "Explicit invariant conflict requires adjudication", conflicts)
            cursor = self.s.execute("UPDATE artifacts SET status='accepted' WHERE id=? AND revision=? AND status='draft'",
                                    (artifact,expected_revision))
            need(cursor.rowcount == 1, "stale_revision", "Artifact changed concurrently")
            self.sec.event(row['project'], "artifact_accepted", actor.id, {"id": artifact,"revision": expected_revision,"digest":row['digest'],"review":review_receipt})
        return self.artifact(actor, artifact)

    def explicit_conflicts(self, project, body, exclude=()):
        constraints = body.get('constraints', {})
        result = []
        if not constraints:
            return result
        for row in self.s.all("SELECT id,digest,body FROM artifacts WHERE project=? AND status='accepted'", (project,)):
            if row['id'] in exclude:
                continue
            existing = parse_json(row['body']).get('constraints', {})
            for key in constraints.keys() & existing.keys():
                if constraints[key] != existing[key]:
                    result.append({"artifact": row['id'], "digest": row['digest'], "key": key, "existing": existing[key], "proposed": constraints[key]})
        return result

    def link(self, actor, source: str, target: str, relation: str, confidence="inferred", basis="", review_receipt=None):
        with self.s.transaction():
            a, b = self.artifact(actor, source), self.artifact(actor, target)
            actor.require("owner", "agent", project=a['project'])
            need(a['project'] == b['project'], "cross_project", "Trace cannot cross projects")
            need(relation in RELATIONS and source != target and confidence in {'inferred','asserted'}, "invalid_link", "Invalid relation")
            text(basis, "link basis", 12000)
            if relation == 'decomposes':
                existing = self.s.one("SELECT source FROM links WHERE target=? AND relation='decomposes' AND source!=?", (target,source))
                need(not existing, "multiple_parents", "Use typed links, not multiple hierarchical parents")
                cycle = self.s.one("WITH RECURSIVE descendants(id) AS (SELECT target FROM links WHERE source=? AND relation='decomposes' UNION SELECT l.target FROM links l JOIN descendants d ON l.source=d.id WHERE l.relation='decomposes') SELECT id FROM descendants WHERE id=?", (target,source))
                need(not cycle, "cycle", "Hierarchy would become cyclic")
            previous = self.s.one("SELECT confidence FROM links WHERE source=? AND target=? AND relation=?", (source,target,relation))
            if confidence == 'asserted' and actor.role != 'owner':
                need(review_receipt and self.assessments, "review_required", "Asserted links need independent verification")
                review = self.assessments.receipt(review_receipt)
                role = review.get('role')
                need(role in {'trace','design'}, 'stale_evidence', 'Asserted link approval has the wrong review role')
                proposal = {'format':'artifact.link.v1','target':target,'relation':relation,
                            'confidence':'asserted','basis':basis}
                materials = getattr(self.assessments, 'review_materials', None)
                need(materials is not None and callable(getattr(materials, 'artifact_link', None)),
                     'review_material_unavailable', 'Current asserted-link review material is unavailable')
                material = materials.artifact_link(actor, source, role, proposal)
                self.assessments.require_review(review_receipt, source, material['binding'],
                                                {role}, latest=True)
            self.s.execute("INSERT INTO links VALUES(?,?,?,?,?) ON CONFLICT(source,target,relation) DO UPDATE SET confidence=excluded.confidence,basis=excluded.basis", (source,target,relation,confidence,basis))
            self.sec.event(a['project'], "link_recorded", actor.id, {"source":source,"target":target,"relation":relation,"confidence":confidence,"previous":previous})
        return {"source":source,"target":target,"relation":relation,"confidence":confidence}

    def impact(self, actor, project, artifacts):
        self.project(actor, project)
        strings(artifacts, "artifacts", nonempty=True)
        for x in artifacts:
            need(self.artifact(actor,x)['project']==project,"cross_project","Invalid impact root")
        # Reachability is computed inside SQLite; never load millions of edges into Python.
        # UNION (not UNION ALL) deduplicates nodes and terminates cyclic non-hierarchical graphs.
        values=','.join('(?)' for _ in artifacts)
        cte=f"""WITH RECURSIVE affected(id) AS (
            VALUES {values}
            UNION SELECT l.source FROM links l JOIN affected a ON l.target=a.id
            UNION SELECT l.target FROM links l JOIN affected a ON l.source=a.id WHERE l.relation='decomposes'
        )"""
        params=tuple(artifacts)
        found=[r['id'] for r in self.s.all(cte+' SELECT id FROM affected ORDER BY id',params)]
        tasks_cte=cte+""", affected_tasks(id) AS (
            SELECT r.task FROM task_reads r JOIN affected a ON r.artifact=a.id
            UNION SELECT d.task FROM task_deps d JOIN affected_tasks t ON d.dependency=t.id
        ) SELECT id FROM affected_tasks ORDER BY id"""
        task_ids=[r['id'] for r in self.s.all(tasks_cte,params)]
        sample=self.s.all(cte+""" SELECT l.* FROM links l JOIN affected a ON a.id=l.target
            WHERE l.confidence='inferred' ORDER BY l.target,l.source,l.relation LIMIT 201""",params)
        # The bounded inferred-link sample is diagnostic evidence only.  The
        # complete target identities below are the material consumed by the
        # Unit 2c-4 impact denominator; status/epoch/attempt telemetry is
        # deliberately excluded from these refs.
        artifact_rows = {row['id']: row for row in self.s.all(
            "SELECT id,revision,digest FROM artifacts WHERE project=?", (project,))}
        task_rows = {row['id']: row for row in self.s.all(
            "SELECT id,revision,body FROM tasks WHERE project=?", (project,))}
        artifact_refs = [{"kind": "artifact", "project": project,
                          "artifact": ident, "revision": artifact_rows[ident]['revision'],
                          "body_digest": artifact_rows[ident]['digest']}
                         for ident in found if ident in artifact_rows]
        task_refs = [{"kind": "task_revision", "project": project,
                      "task": ident, "revision": task_rows[ident]['revision'],
                      "definition_digest": digest(parse_json(task_rows[ident]['body']))}
                     for ident in task_ids if ident in task_rows]
        return {'artifacts':found,'tasks':task_ids,
                'artifact_refs':artifact_refs,'task_refs':task_refs,
                'inferred_links':sample[:200],
                'inferred_links_are_sample':len(sample)>200,'reachable_sets_complete':True,
                'unknown':'Registered-graph reachability only. Undeclared, dynamic and unindexed consumers require discovery; sample limits never truncate the affected artifact/task sets.'}

    def invalidate(self, project, roots, reason):
        impact = self.impact(Actor("system","owner"), project, roots)
        for task in impact['tasks']:
            self.s.execute("UPDATE tasks SET validity='needs_review',epoch=epoch+1,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=? AND status!='cancelled'",(timestamp(),task))
            for root in roots:
                self.s.execute("INSERT OR REPLACE INTO blocks VALUES(?,?,?,?)",(task,'changed_input',root,reason))
        return impact

    def trace(self, actor, project):
        self.project(actor, project)
        missing = []
        rows = self.s.all("SELECT id FROM artifacts a WHERE a.project=? AND a.kind='requirement' AND a.status='accepted' AND NOT EXISTS(SELECT 1 FROM links l JOIN artifacts child ON child.id=l.target WHERE l.source=a.id AND l.relation='decomposes' AND l.confidence='asserted' AND child.kind='requirement' AND child.status='accepted')", (project,))
        required = self.s.all("SELECT id,body FROM artifacts WHERE project=? AND kind='requirement' AND status='accepted' ORDER BY id", (project,))
        for r in required:
            links = self.s.all("SELECT l.relation,a.kind,a.id,l.confidence FROM links l JOIN artifacts a ON a.id=l.source WHERE l.target=? AND a.status='accepted'", (r['id'],))
            tasks = self.s.all("SELECT t.id,t.status,t.validity,t.body FROM tasks t JOIN task_reads r ON r.task=t.id WHERE r.artifact=? AND t.status!='cancelled'",(r['id'],))
            gaps=[]
            if not any(l['relation']=='realizes' and l['kind'] in {'design','component'} and l['confidence']=='asserted' for l in links): gaps.append('design')
            if not any(l['relation']=='verifies' and l['kind']=='test' and l['confidence']=='asserted' for l in links): gaps.append('verification_design')
            if not tasks: gaps.append('task')
            from .obligations import from_store
            covered = set()
            for task in tasks:
                body = parse_json(task['body'])
                if body.get('task_kind') != 'production': continue
                try:
                    covered.update(ac for req,ac in from_store(self.s,body)['pairs'] if req==r['id'])
                except Fault as exc:
                    gaps.append('task_acceptance:'+task['id']+':'+exc.code)
            for ac in parse_json(r['body'])['acceptance']:
                if ac not in covered: gaps.append('acceptance_task:'+ac)
            if gaps: missing.append({"requirement":r['id'],"missing":gaps})
        return {"leaf_count":len(rows),"requirement_count":len(required),"missing":missing,"structural_complete":bool(rows) and not missing,
                "implementation_verified":False,"note":"Trace presence is not proof of implementation or test adequacy."}

    def progress_epoch(self, project):
        # Source classification and link-only work are genuine progress, although
        # neither changes artifact rows. Review/run events do not self-invalidate.
        kinds=('source_registered','source_classified','link_recorded','document_imported','document_extraction_adopted')
        marks=','.join('?' for _ in kinds)
        return self.s.one(f"SELECT COALESCE(MAX(seq),0) AS seq FROM events WHERE project=? AND kind IN ({marks})", (project,*kinds))['seq']

    def baseline(self, actor, project, layout='auto', chunk_bytes=1024*1024):
        from .knowledge_history import KnowledgeHistory
        return KnowledgeHistory(self.s,self.sec,self).create(actor,project,layout,chunk_bytes)

    def export(self, actor, project):
        from .knowledge_history import KnowledgeHistory
        return KnowledgeHistory(self.s,self.sec,self).export_current(actor,project)
