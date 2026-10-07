"""Reviewed local production execution proposals.

Local execution is a second, explicitly recorded readiness route for a canonical
production Task.  It does not create a second planning authority: the proposal
freezes semantic inputs, packets are lossless views of those inputs, and only
observed feasibility/impact reviews can certify the proposal.  Runtime values
such as leases, attempts and candidates are deliberately kept out of the
proposal material so ordinary execution does not invalidate its own authority.
"""
from __future__ import annotations

from collections import defaultdict

from .common import Fault, canonical, digest, need, obj, parse_json, strings, text, timestamp, uid
from .packets import slices
from .obligations import marker as obligation_marker


FORMAT = "daikibo.local-execution.v1"
PACKET_FORMAT = "daikibo.local-execution-review.v1"
STAGES = ("requirements", "scenarios", "boundaries", "contracts", "feasibility", "design", "plan")
ROLES = ("feasibility", "impact")
# Stage evidence may come from an accepted canonical input or an observed
# review of that input/analysis.  These are deliberately broad existing review
# roles; the subject/binding checks below provide the authority boundary.
STAGE_REVIEW_ROLES = {
    "requirements": {"requirements", "phase", "spec", "consistency"},
    "scenarios": {"requirements", "phase", "spec", "design", "feasibility"},
    "boundaries": {"requirements", "phase", "design", "consistency"},
    "contracts": {"phase", "design", "consistency"},
    "feasibility": {"phase", "feasibility", "spec", "quality"},
    "design": {"phase", "design", "trace", "consistency"},
    "plan": {"phase", "design", "trace", "test_plan", "consistency"},
}
MAX_PROPOSAL_BYTES = 128 * 1024 * 1024
MAX_TASKS = 10000
MAX_INVENTORY = 100000

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_execution_proposals(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 program TEXT NOT NULL REFERENCES programs(id), subplan TEXT NOT NULL REFERENCES subplans(id),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS local_execution_proposals_program
 ON local_execution_proposals(program,created,id);
CREATE TABLE IF NOT EXISTS local_execution_packets(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id), ordinal INTEGER NOT NULL,
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), UNIQUE(proposal,ordinal)
);
CREATE INDEX IF NOT EXISTS local_execution_packets_proposal
 ON local_execution_packets(proposal,ordinal);
CREATE TABLE IF NOT EXISTS local_execution_records(
 id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
 proposal TEXT NOT NULL REFERENCES local_execution_proposals(id), task TEXT REFERENCES tasks(id),
 epoch INTEGER, kind TEXT NOT NULL CHECK(kind IN ('certified','withdrawn','claimed','invalidated')),
 digest TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS local_execution_records_proposal
 ON local_execution_records(proposal,created,id);
CREATE INDEX IF NOT EXISTS local_execution_records_task
 ON local_execution_records(task,epoch,kind,created,id);
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_certified
 ON local_execution_records(proposal) WHERE kind='certified';
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_withdrawn
 ON local_execution_records(proposal) WHERE kind='withdrawn';
CREATE UNIQUE INDEX IF NOT EXISTS local_execution_records_task_event
 ON local_execution_records(proposal,task,epoch,kind) WHERE task IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS local_execution_proposals_immutable BEFORE UPDATE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'immutable local execution proposal'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_proposals_no_delete BEFORE DELETE ON local_execution_proposals
 BEGIN SELECT RAISE(ABORT,'retain local execution proposal history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_packets_immutable BEFORE UPDATE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'immutable local execution packet'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_packets_no_delete BEFORE DELETE ON local_execution_packets
 BEGIN SELECT RAISE(ABORT,'retain local execution packet history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_immutable BEFORE UPDATE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'immutable local execution record'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_no_delete BEFORE DELETE ON local_execution_records
 BEGIN SELECT RAISE(ABORT,'retain local execution record history'); END;
CREATE TRIGGER IF NOT EXISTS local_execution_records_shape BEFORE INSERT ON local_execution_records
 WHEN (NEW.kind IN ('certified','withdrawn') AND (NEW.task IS NOT NULL OR NEW.epoch IS NOT NULL))
   OR (NEW.kind IN ('claimed','invalidated') AND (NEW.task IS NULL OR NEW.epoch IS NULL OR NEW.epoch < 0))
 BEGIN SELECT RAISE(ABORT,'invalid local execution record shape'); END;
"""


def _empty_snapshot():
    value = {"format": "snapshot.v1", "repos": {}}
    value["digest"] = digest(value)
    return value


class LocalExecutions:
    def __init__(self, control):
        self.c = control
        self.s = control.s
        self.c.g.local_executions = self

    # ---------- canonical input collection ----------

    def _proposal_row(self, actor, ident):
        row = self.s.one("SELECT * FROM local_execution_proposals WHERE id=?", (ident,), True)
        self.c.k.project(actor, row["project"])
        body = parse_json(row["body"], limit=MAX_PROPOSAL_BYTES)
        need(digest(body) == row["digest"], "integrity_error", "Local execution proposal content differs")
        return row | {"body": body}

    def _program(self, actor, ident):
        row = self.s.one("SELECT * FROM programs WHERE id=?", (ident,), True)
        self.c.k.project(actor, row["project"])
        return row

    def _subplan_tree(self, actor, ident):
        row = self.c.subplans._row(actor, ident)
        return self.c.subplans._tree(actor, ident)

    def _task_definition(self, actor, task):
        row = self.c.w.task(actor, task)
        body = row["body"]
        plan = self.s.one("SELECT body,digest,approved FROM plans WHERE task=?", (task,))
        reads = self.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact", (task,))
        deps = [r["dependency"] for r in self.s.all("SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency", (task,))]
        return {"id": task, "revision": row["revision"], "body": body, "reads": reads,
                "dependencies": deps,
                "test_plan": ({**plan, "body": parse_json(plan["body"])} if plan else None)}

    @staticmethod
    def _task_semantic_digest(definition):
        return digest({"id": definition["id"], "revision": definition["revision"],
                       "body": definition["body"], "reads": definition["reads"],
                       "dependencies": definition["dependencies"], "test_plan": definition["test_plan"]})

    def _stage_artifact_ids(self, actor, subplan, tasks):
        """Return the canonical artifacts a stage reference may name.

        Local evidence is scoped to the selected Task's declared inputs and the
        reviewed subplan context.  An arbitrary accepted artifact in the same
        project is not sufficient evidence merely because it is readable.
        """
        result=set()
        for task in tasks:
            result.update(self.c.w.task(actor,task)["body"].get("read_artifacts", []))
        for row in self._subplan_tree(actor,subplan):
            body=row["body"]
            result.update(body.get("context_artifacts", []))
            for unit in body.get("units", []):
                result.update(x for x in (unit.get("domain"), *unit.get("interfaces", [])) if x)
                result.update(item.get("requirement") for item in unit.get("obligations", []) if item.get("requirement"))
        return result

    def _stage_source_ids(self, actor, artifact_ids):
        result=set()
        for ident in artifact_ids:
            row=self.s.one("SELECT body FROM artifacts WHERE id=?",(ident,))
            if row:
                result.update(parse_json(row["body"]).get("source_refs", []))
        return result

    def _stage_subject_binding(self, actor, project, subject, ensure_policy=True):
        """Resolve the current binding for an observed stage-review subject."""
        artifact=self.s.one("SELECT project,digest,status FROM artifacts WHERE id=?",(subject,))
        if artifact:
            need(artifact["project"]==project and artifact["status"]=="accepted", "stale_evidence", "Stage artifact is not a current accepted input", subject)
            return artifact["digest"], "artifact"
        task=self.s.one("SELECT project,body FROM tasks WHERE id=?",(subject,))
        if task:
            need(task["project"]==project, "cross_project", "Stage review Task belongs elsewhere", subject)
            definition=self._task_definition(actor,subject)
            return self.c.g.task_binding(subject, ensure_policy=ensure_policy), "task"
        packet=self.s.one("SELECT project,digest,subplan FROM subplan_packets WHERE id=?",(subject,))
        if packet:
            need(packet["project"]==project, "cross_project", "Stage review packet belongs elsewhere", subject)
            return packet["digest"], "subplan_packet"
        packet=self.s.one("SELECT project,digest,body FROM breakdown_packets WHERE id=?",(subject,))
        if packet:
            need(packet["project"]==project, "cross_project", "Stage review packet belongs elsewhere", subject)
            return packet["digest"], "breakdown_packet"
        program=self.s.one("SELECT project FROM programs WHERE id=?",(subject,))
        if program:
            need(program["project"]==project, "cross_project", "Stage review program belongs elsewhere", subject)
            return self.c.p.program_binding(subject), "program"
        source=self.s.one("SELECT project,blob FROM sources WHERE id=?",(subject,))
        if source:
            need(source["project"]==project, "cross_project", "Stage source belongs elsewhere", subject)
            return source["blob"], "source"
        raise Fault("invalid_evidence", "Stage review receipt names no canonical subject", subject)

    def _stage_receipt_binding(self, actor, project, receipt_id, *, observed=None,
                               ensure_policy=True):
        """Resolve review material separately from the subject's identity.

        Stage packets and immutable subjects keep their existing digest
        contract. Mutable artifacts and Tasks instead use the exact material
        family that Runtime supplied to the reviewer, including a link
        proposal or frozen test-plan baseline when present.
        """
        observed = observed or self.c.g.receipt(receipt_id)
        need(observed.get("project") == project,
             "cross_project", "Stage review receipt belongs elsewhere", receipt_id)
        subject, role = observed.get("subject"), observed.get("role")
        need(isinstance(subject, str) and isinstance(role, str),
             "invalid_evidence", "Stage review receipt has no canonical subject or role", receipt_id)
        identity_binding, subject_kind = self._stage_subject_binding(
            actor, project, subject, ensure_policy=ensure_policy,
        )
        if subject_kind in {"artifact", "task"}:
            materials = getattr(self.c.g, "review_materials", None)
            need(materials is not None and callable(getattr(materials, "receipt_binding", None)),
                 "review_material_unavailable",
                 "Canonical Runtime review material is unavailable for this stage receipt")
            binding = materials.receipt_binding(
                actor, observed, ensure_policy=ensure_policy,
            )
            need(isinstance(binding, str), "stale_evidence",
                 "Stage review material is no longer current", receipt_id)
            return binding, subject_kind
        return identity_binding, subject_kind

    def _source_inventory(self, project):
        result = []
        for row in self.s.all("SELECT id,project,blob,locator,characters,trust FROM sources WHERE project=? ORDER BY id", (project,)):
            item = {"id": "source:" + row["id"], "kind": "source", "source": row["id"],
                    "digest": row["blob"], "project": project,
                    "value": {k: row[k] for k in ("id", "project", "blob", "locator", "characters", "trust")}}
            result.append(item)
            for disposition in self.s.all("SELECT id,start,end,category,refs,reason,actor FROM dispositions WHERE source=? ORDER BY start,id", (row["id"],)):
                dbody = {**disposition, "refs": parse_json(disposition["refs"])}
                result.append({"id": "source-disposition:" + disposition["id"], "kind": "source_disposition",
                               "source": row["id"], "digest": digest(dbody), "project": project, "value": dbody})
            # Keep unclassified intervals as semantic inventory.  A proposal may
            # explicitly explain why one is independent, but certification will
            # reject an unresolved interval.
            spans = self.s.all("SELECT start,end FROM dispositions WHERE source=? ORDER BY start", (row["id"],))
            cursor = 0
            for span in spans:
                if span["start"] > cursor:
                    ident = f"source-unclassified:{row['id']}:{cursor}:{span['start']}"
                    value = {"source": row["id"], "start": cursor, "end": span["start"]}
                    result.append({"id": ident, "kind": "source_unclassified", "source": row["id"],
                                   "digest": digest(value), "project": project, "value": value})
                cursor = max(cursor, span["end"])
            if cursor < row["characters"]:
                value = {"source": row["id"], "start": cursor, "end": row["characters"]}
                result.append({"id": f"source-unclassified:{row['id']}:{cursor}:{row['characters']}",
                               "kind": "source_unclassified", "source": row["id"],
                               "digest": digest(value), "project": project, "value": value})
        return result

    def _draft_produced_artifacts(self, actor, project, tasks):
        """Return valid draft outputs already produced by selected Tasks.

        A local proposal freezes the semantic project inputs before execution.
        The collector necessarily adds a draft Knowledge artifact afterwards;
        that expected output must not invalidate the proposal that authorized
        its producing Task.  Only a fully validated controller-owned
        artifact_production material for one of the selected current Tasks is
        excluded.  Unrelated or malformed draft rows remain in the inventory
        and therefore still invalidate stale local authorization.
        """
        assurance = getattr(self.c, "assurance", None)
        context = getattr(assurance, "_candidate_context", None)
        if assurance is None or context is None:
            return set()
        selected = {item for item in tasks if isinstance(item, str)}
        if not selected:
            return set()
        from .artifact_provenance import (
            resolve_artifact_production_material,
            resolve_produced_artifact,
        )
        result = set()
        rows = self.s.all(
            "SELECT * FROM assurance_objects WHERE project=? AND kind='material' ORDER BY id",
            (project,),
        )
        for row in rows:
            try:
                envelope = parse_json(row["body"])
                if (not isinstance(envelope, dict) or
                        envelope.get("material_kind") != "artifact_production"):
                    continue
                payload = parse_json(self.s.blob_get(envelope["payload_blob"]))
                task_ref = payload.get("task_ref") if isinstance(payload, dict) else None
                artifact_ref = payload.get("artifact_ref") if isinstance(payload, dict) else None
                if (not isinstance(task_ref, dict) or task_ref.get("task") not in selected or
                        not isinstance(artifact_ref, dict)):
                    continue
                checked = resolve_artifact_production_material(
                    self.s, project=project, artifact_ref=artifact_ref,
                    task_ref=task_ref, context=context,
                    resolve_artifact=lambda ref: resolve_produced_artifact(
                        self.s, ref, project=project, code="stale_reference", current=True,
                    ),
                    blob_get=self.s.blob_get, code="stale_reference",
                    missing_code="artifact_producer_material_missing", current=True,
                )
                produced = checked.get("artifact_ref")
                if (isinstance(produced, dict) and
                        produced.get("project") == project and
                        isinstance(produced.get("artifact"), str)):
                    artifact = self.s.one(
                        "SELECT status FROM artifacts WHERE id=? AND project=?",
                        (produced["artifact"], project),
                    )
                    if artifact is not None and artifact.get("status") == "draft":
                        result.add(produced["artifact"])
            except (Fault, KeyError, TypeError, ValueError):
                # A malformed material cannot grant the expected-output
                # exemption; the underlying draft remains inventory input.
                continue
        return result

    def _artifact_inventory(self, actor, project, tasks):
        expected_outputs = self._draft_produced_artifacts(actor, project, tasks)
        result = []
        for row in self.s.all("SELECT id,project,kind,revision,status,body,digest FROM artifacts WHERE project=? ORDER BY id", (project,)):
            if row["id"] in expected_outputs and row["status"] == "draft":
                continue
            body = parse_json(row["body"])
            value = {"id": row["id"], "project": project, "kind": row["kind"],
                     "revision": row["revision"], "status": row["status"], "digest": row["digest"], "body": body}
            result.append({"id": "artifact:" + row["id"], "kind": "artifact", "artifact": row["id"],
                           "digest": digest(value), "project": project, "value": value})
            if row["kind"] == "requirement":
                for acceptance in body.get("acceptance", []):
                    obligation = {"requirement": row["id"], "acceptance": acceptance,
                                  "revision": row["revision"], "digest": row["digest"], "status": row["status"]}
                    result.append({"id": "obligation:" + row["id"] + ":" + acceptance,
                                   "kind": "obligation", "requirement": row["id"],
                                   "digest": digest(obligation), "project": project, "value": obligation})
            for key, value_item in sorted(body.get("constraints", {}).items()):
                constraint = {"artifact": row["id"], "revision": row["revision"], "digest": row["digest"],
                              "key": key, "value": value_item}
                result.append({"id": f"constraint:{row['id']}:{key}", "kind": "constraint",
                               "artifact": row["id"], "digest": digest(constraint), "project": project,
                               "value": constraint})
            if row["kind"] == "interface":
                consumers = body.get("consumers", [])
                for consumer in sorted(consumers):
                    value_item = {"interface": row["id"], "consumer": consumer, "revision": row["revision"],
                                  "digest": row["digest"]}
                    result.append({"id": f"consumer:{row['id']}:{consumer}", "kind": "consumer",
                                   "interface": row["id"], "digest": digest(value_item), "project": project,
                                   "value": value_item})
        return result

    def _resolve_ref(self, actor, project, ref):
        if isinstance(ref, str):
            ident, expected = ref, None
        else:
            obj(ref, required=("id",), optional=("digest", "revision", "kind"), name="evidence reference")
            ident, expected = ref["id"], ref.get("digest")
        text(ident, "evidence reference", 512)
        row = self.s.one("SELECT id,kind,project,revision,status,digest FROM artifacts WHERE id=?", (ident,))
        kind = "artifact"
        if row is None:
            row = self.s.one("SELECT id,project,subject,role,binding,run FROM receipts WHERE id=?", (ident,))
            kind = "receipt"
        if row is None:
            row = self.s.one("SELECT id,project,blob,locator,characters FROM sources WHERE id=?", (ident,))
            kind = "source"
        need(row and row["project"] == project, "missing_evidence", "Evidence reference is absent or belongs elsewhere", ident)
        if expected is not None:
            actual = row.get("digest") or row.get("binding") or row.get("blob")
            need(actual == expected, "stale_evidence", "Evidence reference digest/binding differs", ident)
        if isinstance(ref, dict) and ref.get("revision") is not None:
            need(kind == "artifact" and row["revision"] == ref["revision"], "stale_evidence", "Evidence artifact revision differs", ident)
        if isinstance(ref, dict) and ref.get("kind") is not None:
            need(ref["kind"] == kind, "invalid_evidence", "Evidence reference type differs", ident)
        result = {"id": ident, "kind": kind}
        if kind == "artifact":
            need(row["status"] == "accepted", "invalid_evidence", "Stage/disposition evidence must reference an accepted artifact", ident)
            result.update({"revision": row["revision"], "digest": row["digest"], "status": row["status"]})
        elif kind == "receipt":
            observed = self.c.g.receipt(ident)
            need(observed["project"] == project and observed["exit_code"] == 0
                 and not any(observed.get(k) for k in ("timed_out", "cancelled", "output_overflow", "failure")),
                 "invalid_evidence", "Evidence receipt is not a successful observed run", ident)
            need(observed.get("result", {}).get("verdict") == "pass"
                 and observed.get("judgment_valid") is True
                 and observed.get("readonly_verified") is True,
                 "invalid_evidence", "Stage receipt must contain an observed passing review", ident)
            result.update({"run": row["run"], "subject": row["subject"], "role": row["role"],
                           "binding": row["binding"], "verdict": observed["result"]["verdict"],
                           "judgment_valid": observed["judgment_valid"],
                           "readonly_verified": observed["readonly_verified"]})
            subject_task=self.s.one("SELECT id FROM tasks WHERE id=?",(row["subject"],))
            if subject_task:
                result["task_semantic_digest"]=self._task_semantic_digest(self._task_definition(actor,row["subject"]))
        else:result.update({"digest": row["blob"], "locator": row["locator"]})
        return result

    def _validate_stage_reference(self, actor, project, program, subplan, tasks, stage, reference,
                                  documented_subjects=()):
        allowed_artifacts=self._stage_artifact_ids(actor,subplan,tasks)
        allowed_sources=self._stage_source_ids(actor,allowed_artifacts)
        tree_ids={row["id"] for row in self._subplan_tree(actor,subplan)}
        if reference["kind"] == "artifact":
            need(reference["id"] in allowed_artifacts or reference["id"] in documented_subjects,
                 "invalid_evidence", "Stage artifact is not a declared Task/subplan input", reference["id"])
            return None
        if reference["kind"] == "source":
            need(reference["id"] in allowed_sources or reference["id"] in documented_subjects,
                 "invalid_evidence", "Stage source is not grounded by a declared canonical input", reference["id"])
            return None
        if reference["kind"] != "receipt":
            raise Fault("invalid_evidence", "Unsupported stage evidence reference", reference)
        subject=reference["subject"]
        _,subject_kind=self._stage_subject_binding(actor,project,subject)
        if subject_kind == "artifact":
            need(subject in allowed_artifacts or subject in documented_subjects,
                 "invalid_evidence","Stage review artifact is outside the selected canonical inputs",subject)
        elif subject_kind == "source":
            need(subject in allowed_sources or subject in documented_subjects,
                 "invalid_evidence","Stage review source is outside the selected canonical inputs",subject)
        elif subject_kind == "task":
            task_row=self.s.one("SELECT body,status,validity FROM tasks WHERE id=?",(subject,),True)
            task_body=parse_json(task_row["body"])
            need(subject in tasks or task_body.get("task_kind")=="analysis",
                 "invalid_evidence", "Stage review Task must be selected or a documented analysis Task", subject)
            if task_body.get("task_kind")=="analysis":
                need(task_row["status"]=="completed" and task_row["validity"]=="current",
                     "invalid_evidence", "Feasibility evidence needs a completed current analysis Task", subject)
        elif subject_kind == "subplan_packet":
            packet=self.s.one("SELECT subplan FROM subplan_packets WHERE id=?",(subject,),True)
            need(packet["subplan"] in tree_ids,"invalid_evidence","Stage review packet is outside the selected subplan",subject)
        elif subject_kind == "breakdown_packet":
            packet=self.s.one("SELECT body FROM breakdown_packets WHERE id=?",(subject,),True)
            need(parse_json(packet["body"]).get("program")==program,"invalid_evidence","Stage review packet belongs to another program",subject)
        elif subject_kind == "program":
            need(subject==program,"invalid_evidence","Stage review program differs from the local program",subject)
        need(reference.get("role") in STAGE_REVIEW_ROLES[stage],
             "invalid_evidence", "Stage review role is not valid for this stage", [stage,reference.get("role")])
        # Validate the observed receipt against its exact current subject
        # binding at proposal creation.  Later runtime claim/epoch/candidate
        # changes are excluded from semantic material and are checked through
        # Task semantic digests instead of self-invalidating this evidence.
        binding, _ = self._stage_receipt_binding(actor, project, reference["id"])
        self.c.g.require_review(reference["id"],subject,binding,{reference["role"]},latest=True)
        return None

    def _normalize_stage_evidence(self, actor, project, program, subplan, tasks, value):
        need(isinstance(value, dict), "invalid_stage_evidence", "Stage evidence must be an object")
        need(set(value) == set(tasks), "invalid_stage_evidence", "Every selected Task needs stage evidence", sorted(set(tasks) - set(value)))
        result = {}
        for task in tasks:
            task_value = value.get(task)
            need(isinstance(task_value, dict), "invalid_stage_evidence", "Stage evidence must be supplied for every Task", task)
            need(set(task_value) == set(STAGES), "invalid_stage_evidence", "Stage evidence must contain exactly the seven required stages", task)
            normalized = {}
            for stage in STAGES:
                entry = task_value.get(stage)
                obj(entry, required=("refs", "explanation"), optional=("review", "role"), name="stage evidence")
                refs, explanation = entry["refs"], entry["explanation"]
                need(isinstance(refs, list) and refs, "invalid_stage_evidence", "Stage evidence needs at least one canonical reference", [task,stage])
                text(explanation, "stage evidence explanation", 12000)
                normalized_refs=[self._resolve_ref(actor, project, ref) for ref in refs]
                review = None
                if entry.get("review") is not None:
                    review = self._resolve_ref(actor, project, entry["review"])
                    need(review["kind"] == "receipt", "invalid_stage_evidence",
                         "The explicit stage review must be an observed receipt", [task, stage])
                # An accepted artifact/source outside the selected read set is
                # usable only when the caller also names an observed review of
                # that exact canonical subject.  This is the typed provenance
                # path for scenario/design evidence; it is not a project-wide
                # accepted-artifact shortcut.
                documented_subjects={review["subject"]} if review is not None else set()
                for resolved in normalized_refs:
                    self._validate_stage_reference(actor,project,program,subplan,tasks,stage,resolved,documented_subjects)
                normalized[stage] = {"refs":normalized_refs, "explanation": explanation}
                if review is not None:
                    # Validate the review after deriving the documented subject
                    # so an accepted scenario artifact can be reviewed in its
                    # own current artifact binding.
                    self._validate_stage_reference(actor,project,program,subplan,tasks,stage,review,documented_subjects)
                    normalized[stage]["review"] = review
                receipt_roles={resolved["role"] for resolved in normalized_refs + ([review] if review is not None else [])
                               if resolved["kind"] == "receipt"}
                declared_role=entry.get("role")
                if declared_role is not None:
                    need(declared_role in STAGE_REVIEW_ROLES[stage], "invalid_stage_evidence", "Unknown stage review role")
                    need(receipt_roles <= {declared_role}, "invalid_stage_evidence",
                         "Declared stage review role differs from observed receipt", [task,stage])
                elif len(receipt_roles)>1:
                    need(False, "invalid_stage_evidence", "Stage evidence needs one explicit observed review role", [task,stage])
                if receipt_roles:
                    # Preserve the actual managed review role in the immutable
                    # proposal even when the caller omitted the shorthand.
                    normalized[stage]["role"] = declared_role or sorted(receipt_roles)[0]
            result[task] = normalized
        return result

    def _audit_stage_evidence(self, actor, project, tasks, stage_evidence, ensure_policy=True):
        """Recheck semantic subjects and observed review verdicts after propose."""
        for task, stages in stage_evidence.items():
            for stage, entry in stages.items():
                refs=list(entry.get("refs", []))
                if entry.get("review") is not None:
                    refs.append(entry["review"])
                for reference in refs:
                    if reference["kind"] == "artifact":
                        row=self.s.one("SELECT digest,revision,status,project FROM artifacts WHERE id=?",(reference["id"],))
                        need(row and row["project"]==project and row["status"]=="accepted"
                             and row["digest"]==reference.get("digest") and row["revision"]==reference.get("revision"),
                             "stale_evidence", "Stage artifact changed after proposal", reference["id"])
                    elif reference["kind"] == "source":
                        row=self.s.one("SELECT blob,project FROM sources WHERE id=?",(reference["id"],))
                        need(row and row["project"]==project and row["blob"]==reference.get("digest"),
                             "stale_evidence", "Stage source changed after proposal", reference["id"])
                    elif reference["kind"] == "receipt":
                        observed=self.c.g.receipt(reference["id"])
                        need(observed.get("result",{}).get("verdict")=="pass"
                             and observed.get("judgment_valid") is True
                             and observed.get("readonly_verified") is True,
                             "invalid_evidence", "Stage review no longer has a passing observed verdict", reference["id"])
                        binding, _ = self._stage_receipt_binding(
                            actor, project, reference["id"], observed=observed,
                            ensure_policy=ensure_policy,
                        )
                        need(observed["binding"] == binding and reference.get("binding") == binding,
                             "stale_evidence", "Stage review subject binding changed after proposal", reference["id"])
                        need(observed.get("role") in STAGE_REVIEW_ROLES[stage],
                             "invalid_evidence", "Stage review role is not valid for this stage", [stage, reference.get("role")])
                        self.c.g.require_review(reference["id"], observed["subject"], binding,
                                                {observed["role"]}, latest=True)
                        if reference.get("task_semantic_digest"):
                            task_row=self.s.one("SELECT body,status,validity FROM tasks WHERE id=?",(observed["subject"],),True)
                            task_body=parse_json(task_row["body"])
                            if task_body.get("task_kind")=="analysis":
                                need(task_row["status"]=="completed" and task_row["validity"]=="current",
                                     "stale_evidence", "Stage analysis Task is no longer completed/current", observed["subject"])
                            definition=self._task_definition(actor,observed["subject"])
                            need(self._task_semantic_digest(definition)==reference["task_semantic_digest"],
                                 "stale_evidence", "Stage analysis Task changed after proposal", observed["subject"])

    def _subplan_coverage(self, actor, subplan):
        root = self.c.subplans._row(actor, subplan)
        pairs = {(o["requirement"], o["acceptance"]) for o in root["body"]["obligations"]}
        units = {t for unit in root["body"]["units"] for t in unit["tasks"]}
        return root, pairs, units

    def _validate_targets(self, actor, program, subplan, tasks):
        flow=self._program(actor,program);project=flow["project"]
        actor.require("owner","agent",project=project);self.c.k.project(actor,project)
        strings(tasks,"tasks",maximum=MAX_TASKS,nonempty=True)
        need(len(tasks)==len(set(tasks)),"duplicate_task","A Task may appear only once")
        subplan_row,covered,subplan_tasks=self._subplan_coverage(actor,subplan)
        need(subplan_row["project"]==project and subplan_row["program"]==program,"cross_program","Subplan belongs to another program")
        for task in tasks:
            row=self.c.w.task(actor,task)
            need(row["project"]==project and row["status"]!="cancelled","invalid_task","Task is missing or cancelled",task)
            need(row["body"].get("task_kind")=="production","invalid_task","Local execution only accepts production Tasks",task)
            need(row["body"].get("workflow_id")==program,"task_program_mismatch","Task belongs to another root program",task)
            need(task in subplan_tasks,"task_scope_mismatch","Task is not present in the subplan",task)
            definition=self._task_definition(actor,task);from .obligations import from_store
            need(set(from_store(self.s,definition["body"])["pairs"])<=covered,"obligation_scope_mismatch","Task obligation is not covered by the subplan",task)
        return flow,project

    def _normalize_dispositions(self, actor, project, tasks, inventory, dispositions):
        need(isinstance(dispositions, (dict,list)), "invalid_dispositions", "Dispositions must be a task map or list")
        rows = []
        if isinstance(dispositions, dict):
            for task, values in dispositions.items():
                need(isinstance(values, (dict,list)), "invalid_dispositions", "Task dispositions must be a map or list", task)
                if isinstance(values, dict):
                    values = [{"item_id": item, **value} for item, value in values.items()]
                for value in values: rows.append({"task": task, **value})
        else: rows = dispositions
        expected = {(task, item["id"]): item for task in tasks for item in inventory}
        # Consumer names are canonical relationships, not free-form labels.
        # Include Task IDs because a selected production Task is a legitimate
        # downstream consumer, and include interface-declared consumers so a
        # disposition can explain an existing named client as well.
        known_consumers={row["id"] for row in self.s.all("SELECT id FROM tasks WHERE project=?",(project,))}
        for row in self.s.all("SELECT body FROM artifacts WHERE project=? AND kind='interface'",(project,)):
            known_consumers.update(parse_json(row["body"]).get("consumers", []))
        known_consumers.update(item.get("value",{}).get("consumer") for item in inventory if item.get("kind")=="consumer")
        known_consumers.discard(None)
        seen = set();normalized = []
        for value in rows:
            obj(value, required=("task", "item_id", "item_digest", "disposition", "reason"), optional=("evidence_refs", "boundary", "consumers", "evidence"), name="disposition")
            task, ident = value["task"], value["item_id"]
            need((task,ident) in expected, "unknown_disposition", "Disposition does not name a selected Task and inventory item", [task,ident])
            need((task,ident) not in seen, "duplicate_disposition", "Disposition appears more than once", [task,ident]);seen.add((task,ident))
            item = expected[(task,ident)]
            need(value["item_digest"] == item["digest"], "stale_disposition", "Disposition inventory digest differs", ident)
            need(value["disposition"] in {"required_resolved", "independent", "unresolved"}, "invalid_disposition", "Unknown disposition classification", value["disposition"])
            text(value["reason"], "disposition reason", 12000)
            refs = value.get("evidence_refs", value.get("evidence", []))
            need(isinstance(refs,list), "invalid_dispositions", "Disposition evidence_refs must be a list")
            need(refs, "invalid_dispositions", "Every disposition needs canonical evidence references")
            refs = [self._resolve_ref(actor, project, ref) for ref in refs]
            boundary = value.get("boundary", "")
            consumers = value.get("consumers", [])
            need(isinstance(boundary,str) and boundary.strip(), "invalid_dispositions", "Boundary explanation is required")
            text(boundary, "boundary explanation", 12000)
            need(isinstance(consumers,list) and all(isinstance(x,str) for x in consumers), "invalid_dispositions", "Consumer explanation must be a string list")
            unknown_consumers=[x for x in consumers if x not in known_consumers]
            # Keep an unknown relationship in the immutable proposal only as an
            # explicit unresolved judgment.  Certification/audit rejects that
            # disposition; free-form boundary language remains review material
            # rather than a language-specific lexical allowlist.
            need(not unknown_consumers or value["disposition"]=="unresolved", "unknown_consumer",
                 "Unknown consumer must remain explicitly unresolved", unknown_consumers)
            if item["kind"] == "consumer":
                need(item["value"].get("consumer") in consumers,
                     "unknown_consumer", "A consumer inventory item needs an explicit matching consumer explanation", ident)
            normalized.append({"id":f"disposition:{task}:{ident}","task":task,"item_id":ident,"item_digest":item["digest"],"disposition":value["disposition"],
                               "reason":value["reason"],"evidence_refs":refs,"boundary":boundary,"consumers":consumers})
        missing = sorted(set(expected) - seen)
        need(not missing, "missing_disposition", "Every inventory item and selected Task needs an explicit disposition", missing[:100])
        need(len(normalized) <= MAX_INVENTORY, "local_execution_capacity", "Disposition inventory exceeds the explicit capacity")
        return sorted(normalized, key=lambda x:(x["task"],x["item_id"]))

    def _collect_material(self, actor, row, body):
        project = row["project"]
        selected = list(body["tasks"])
        # Include the complete transitive prerequisite closure.  A direct-only
        # projection could let a changed second-level prerequisite escape the
        # local independence and completion recheck.
        known=set(selected);pending=list(selected);external_ids=[]
        while pending:
            task_id=pending.pop()
            for dep in self._task_definition(actor,task_id)["dependencies"]:
                if dep not in known:
                    known.add(dep);external_ids.append(dep);pending.append(dep)
        external_ids=sorted(external_ids)
        definitions = {task:self._task_definition(actor,task) for task in selected + external_ids}
        task_items=[]
        for task in sorted(selected + external_ids):
            definition=definitions[task]
            value={"id":task,"revision":definition["revision"],"body":definition["body"],"reads":definition["reads"],
                   "dependencies":definition["dependencies"],"test_plan":definition["test_plan"]}
            task_items.append({"id":("task:" if task in selected else "external-task:")+task,
                               "kind":"task" if task in selected else "external_task","task":task,
                               "digest":self._task_semantic_digest(definition),"project":project,"value":value})
        artifacts=self._artifact_inventory(actor, project, selected)
        sources=self._source_inventory(project)
        inventory=sorted(sources+artifacts+task_items, key=lambda x:x["id"])
        # Freeze the semantic projection of every canonical production Task in
        # the project.  Runtime state (status, epoch, lease, candidate and
        # attempts) is intentionally excluded; a new sibling Task or a change
        # to its scope still invalidates an independence judgment.
        task_inventory=[]
        for task_row in self.s.all("SELECT id FROM tasks WHERE project=? ORDER BY id", (project,)):
            task_id=task_row["id"]
            definition=self._task_definition(actor,task_id)
            task_body=definition["body"]
            if task_body.get("task_kind") != "production":
                continue
            task_inventory.append({"id":task_id,"revision":definition["revision"],
                                   "digest":self._task_semantic_digest(definition),"body":task_body,
                                   "reads":definition["reads"],"dependencies":definition["dependencies"],
                                   "test_plan":definition["test_plan"]})
        # Semantic graph and unresolved project records are separate inventory
        # items so a new consumer/decision/conflict cannot be hidden by a count.
        for row_data in self.s.all("SELECT source,target,relation,confidence,basis FROM links l JOIN artifacts a ON a.id=l.source WHERE a.project=? ORDER BY source,target,relation", (project,)):
            value=dict(row_data);inventory.append({"id":"trace:"+digest(value),"kind":"trace","digest":digest(value),"project":project,"value":value})
        for table,kind in (("decisions","decision"),("conflicts","conflict")):
            for row_data in self.s.all(f"SELECT * FROM {table} WHERE project=? ORDER BY id", (project,)):
                value=dict(row_data)
                for key in ("body","response"):
                    if value.get(key) and isinstance(value[key],str) and key=="body":value[key]=parse_json(value[key])
                inventory.append({"id":f"{kind}:{row_data['id']}","kind":kind,"digest":digest(value),"project":project,"value":value})
        for row_data in self.s.all("SELECT task,kind,ref,reason FROM blocks b JOIN tasks t ON t.id=b.task WHERE t.project=? ORDER BY task,kind,ref", (project,)):
            value=dict(row_data);inventory.append({"id":f"block:{value['task']}:{value['kind']}:{value['ref']}","kind":"block","digest":digest(value),"project":project,"value":value})
        program=self._program(actor,body["program"])
        program_value={"id":program["id"],"project":program["project"],"source":parse_json(program["body"])["source"],
                       "mode":parse_json(program["body"])["mode"]}
        inventory.append({"id":"program:"+program["id"],"kind":"program","digest":digest(program_value),"project":project,"value":program_value})
        policy_row=self.s.one("SELECT * FROM policies WHERE project=?",(project,))
        if policy_row:
            policy_body=parse_json(policy_row["body"]);policy_digest=policy_row["digest"];policy_revision=policy_row["revision"]
        else:
            from .governance import DEFAULT_POLICY
            policy_body=DEFAULT_POLICY.copy();policy_digest=digest(policy_body);policy_revision=1
        inventory.append({"id":"policy:"+project,"kind":"policy","digest":policy_digest,"project":project,
                          "value":{"revision":policy_revision,"digest":policy_digest,"body":policy_body}})
        inventory=sorted(inventory,key=lambda x:x["id"])
        need(len(inventory)<=MAX_INVENTORY,"local_execution_capacity","Semantic inventory exceeds the explicit capacity")
        subplan_rows=self._subplan_tree(actor,body["subplan"])
        subplans=[]
        for partial in subplan_rows:
            subplans.append({"id":partial["id"],"digest":partial["digest"],"program":partial["program"],
                             "material_digest":partial["body"].get("material_digest"),
                             "packets":[{"id":p["id"],"digest":p["digest"]} for p in self.s.all("SELECT id,digest FROM subplan_packets WHERE subplan=? ORDER BY ordinal",(partial["id"],))],
                             "reviews":self.s.all("SELECT id,run,subject,role,binding FROM receipts WHERE subject IN (SELECT id FROM subplan_packets WHERE subplan=?) ORDER BY created,id",(partial["id"],))})
        task_obligations={}
        for task in selected:
            definition=definitions[task];from .obligations import from_store
            task_obligations[task]=sorted([list(x) for x in from_store(self.s,definition["body"])["pairs"]])
        material={"format":FORMAT,"project":project,"program":body["program"],"subplan":body["subplan"],
                  "program_source":program_value,"sources":sources,"inventory":inventory,
                  "tasks":[task_items for task_items in task_items if task_items["kind"]=="task"],
                  "external_tasks":[task_items for task_items in task_items if task_items["kind"]=="external_task"],
                  "task_inventory":task_inventory,
                  "task_obligations":task_obligations,"subplans":subplans,
                  "stage_evidence":body["stage_evidence"],"dispositions":body["dispositions"],
                  "obligations":sorted([list(x) for x in self._subplan_coverage(actor,body["subplan"])[1]]),
                  "policy":policy_digest,
                  "instructions":"This is a local production execution proposal. Inspect every packet and full inventory; local certification never adopts the root plan, resolves unrelated obligations, or proves release readiness."}
        return material

    def _stage_markers(self, body, material):
        result=[]
        for task in body["tasks"]:
            for stage in STAGES:
                result.append("LEXSTAGE-" + digest([task, stage, digest(material["stage_evidence"][task][stage])]))
        return result

    def _obligation_markers(self, material):
        return [obligation_marker(pair[0],pair[1]) for pair in material["obligations"]]

    def _impact_markers(self, body, material):
        result=[]
        for row in body["dispositions"]:
            result.append("LEXIMPACT-" + digest([row["task"],row["item_id"],row["item_digest"],row["disposition"]]))
        return result

    def inventory(self, actor, program, subplan, tasks, offset=0, limit=100):
        """Read-only preparation view for exact disposition construction.

        A proposer must classify every returned item by ID and digest.  This
        endpoint exposes the canonical inventory before a proposal exists; it
        never creates a proposal, packet, receipt, review, or certification.
        """
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 500,
             "invalid_range", "Use a bounded local inventory page")
        with self.s.transaction():
            _,project=self._validate_targets(actor,program,subplan,tasks)
            material=self._collect_material(actor,{"project":project},
                                            {"program":program,"subplan":subplan,"tasks":sorted(tasks),
                                             "stage_evidence":{},"dispositions":{}})
            items=material["inventory"]
            need(offset <= len(items), "invalid_range", "Inventory offset is past the end")
            page=items[offset:offset+limit]
            return {"project":project,"program":program,"subplan":subplan,"tasks":sorted(tasks),
                    "items":page,"total":len(items),"offset":offset,
                    "next_offset":offset+len(page) if offset+len(page)<len(items) else None,
                    "material_digest":digest(material),"task_obligations":material["task_obligations"],
                    "read_only":True,"certification":False}

    # ---------- proposal and packet API ----------

    def propose(self, actor, program, subplan, tasks, rationale, stage_evidence, dispositions,
                byte_budget=24000, request_id=None):
        # Semantic collection and immutable proposal/packet insertion share one
        # serialized transaction.  A concurrent artifact/task change therefore
        # cannot produce a packet whose digest was never observed atomically.
        with self.s.transaction():
            return self._propose(actor, program, subplan, tasks, rationale, stage_evidence,
                                 dispositions, byte_budget, request_id)

    def _propose(self, actor, program, subplan, tasks, rationale, stage_evidence, dispositions,
                 byte_budget=24000, request_id=None):
        flow,project=self._validate_targets(actor,program,subplan,tasks);text(rationale,"rationale",20000)
        need(type(byte_budget) is int and 4096<=byte_budget<=100000,"invalid_budget","Packet budget must be 4096..100000 bytes")
        stage=self._normalize_stage_evidence(actor,project,program,subplan,tasks,stage_evidence)
        # Build a temporary declaration before collecting material.  External
        # prerequisites are derived from the canonical Task graph, never inferred
        # from a prose field supplied by the caller.
        temp={"program":program,"subplan":subplan,"tasks":sorted(tasks),"rationale":rationale,"stage_evidence":stage,"dispositions":[]}
        temp_row={"project":project}
        raw_material=self._collect_material(actor,temp_row,temp)
        temp["dispositions"]=self._normalize_dispositions(actor,project,tasks,raw_material["inventory"],dispositions)
        material=self._collect_material(actor,temp_row,temp)
        material_digest=digest(material)
        # The packet body is the exact canonical semantic material.  It is not a
        # truncated summary; only packet boundaries are transport projections.
        proposal=uid("LEX")
        stage_markers=self._stage_markers(temp,material)
        obligation_markers=self._obligation_markers(material)
        impact_markers=self._impact_markers(temp,material)
        coverage_manifest={"stages":stage_markers,"obligations":obligation_markers,"impact":impact_markers}
        serialized=canonical(material).decode()
        need(len(serialized.encode())<=MAX_PROPOSAL_BYTES,"local_execution_capacity","Local execution material exceeds explicit capacity")
        # Leave room for identity, instructions and coverage metadata.  The
        # packet validator below still checks the exact UTF-8 byte limit.
        chunk_budget=max(256,(byte_budget-1800)//2)
        fragments=list(slices(serialized,chunk_budget));packets=[];manifest=[]
        for ordinal,(start,end,fragment) in enumerate(fragments):
            marker_part="LEXPART-"+digest([proposal,material_digest,start,end])
            assigned=[]
            # Spread semantic markers over packets.  Every marker is also listed
            # in the complete manifest, so a reviewer can request its full item.
            for category,values in coverage_manifest.items():
                # Distribute every marker exactly once across the packet
                # fragments.  The complete manifest remains available in each
                # packet for navigation, while required_coverage stays small.
                if category == "obligations":
                    # Obligations are shared context for every feasibility
                    # review, so retain their markers in every packet.  They
                    # remain deduplicated within a packet and do not replace
                    # the complete-manifest/global coverage checks.
                    assigned.extend(values)
                else:
                    assigned.extend(value for index, value in enumerate(values)
                                    if index % len(fragments) == ordinal)
            required=[]
            for marker_value in [marker_part,*assigned]:
                if marker_value not in required:required.append(marker_value)
            packet={"format":PACKET_FORMAT,"proposal":proposal,"program":program,"subplan":subplan,"ordinal":ordinal,
                    "material_digest":material_digest,"start":start,"end":end,"total_characters":len(serialized),
                    "serialized_fragment":fragment,"required_coverage":required,
                    "coverage_manifest":coverage_manifest,"complete_manifest":{"packet_count":len(fragments),"markers":coverage_manifest},
                    "required_coverage_items":{"stages":stage_markers,"obligations":obligation_markers,"impact":impact_markers},
                    "instructions":"Inspect the complete local execution material through all packets. Feasibility reviews judge selected前段充足/検証可能性; impact reviews judge full-inventory independence, neighbors, consumers and writes. Return blocked for missing context; a local PASS never adopts the root plan or certifies release readiness."}
            encoded=canonical(packet);need(len(encoded)<=byte_budget,"context_insufficient","Packet metadata exceeds byte budget; increase byte_budget")
            pid=uid("LEXPACK");packet["id"]=pid;encoded=canonical(packet)
            need(len(encoded)<=byte_budget,"context_insufficient","Packet metadata exceeds byte budget; increase byte_budget")
            packet_digest=digest(packet)
            packets.append((pid,project,ordinal,packet,packet_digest));manifest.append({"id":pid,"digest":packet_digest})
        stable_payload={"format":FORMAT,"program":program,"subplan":subplan,"project":project,"tasks":sorted(tasks),"rationale":rationale,
                 "stage_evidence":stage,"dispositions":temp["dispositions"],"material_digest":material_digest,
                 "coverage_manifest":coverage_manifest,"task_obligations":material["task_obligations"]}
        payload={**stable_payload,"packet_manifest":manifest,"packet_count":len(manifest),"material_characters":len(serialized),
                 "created_by":actor.id,"payload_digest":digest(stable_payload)}
        if request_id is not None:text(request_id,"request_id",256)
        with self.s.transaction():
            if request_id is not None:
                previous=self.s.one("SELECT * FROM local_execution_proposals WHERE program=? AND json_extract(body,'$.request_id')=? ORDER BY created,id LIMIT 1",(program,request_id))
                if previous:
                    previous_body=parse_json(previous["body"], limit=MAX_PROPOSAL_BYTES)
                    need(previous_body.get("payload_digest")==payload["payload_digest"],"idempotency_conflict","Local execution request ID was reused for different content")
                    return self.get(actor,previous["id"])
            duplicate=self.s.one("SELECT * FROM local_execution_proposals WHERE program=? AND json_extract(body,'$.material_digest')=? ORDER BY created,id LIMIT 1",(program,material_digest))
            if duplicate:
                old=parse_json(duplicate["body"], limit=MAX_PROPOSAL_BYTES);need(old.get("payload_digest")==payload["payload_digest"],"idempotency_conflict","The same semantic material was proposed with different content")
                return self.get(actor,duplicate["id"])
            self.s.execute("INSERT INTO local_execution_proposals VALUES(?,?,?,?,?,?,?)",(proposal,project,program,subplan,digest(payload),canonical(payload).decode(),timestamp()))
            for pid,_,ordinal,packet,h in packets:
                packet["id"]=pid
                self.s.execute("INSERT INTO local_execution_packets VALUES(?,?,?,?,?,?)",(pid,project,proposal,ordinal,h,canonical(packet).decode()))
            self.c.sec.event(project,"local_execution_proposed",actor.id,{"proposal":proposal,"program":program,"subplan":subplan,"tasks":sorted(tasks),"material_digest":material_digest,"packets":len(packets)})
        return self.get(actor,proposal)

    def _packet_rows(self, actor, proposal):
        row=self._proposal_row(actor,proposal)
        rows=self.s.all("SELECT * FROM local_execution_packets WHERE proposal=? ORDER BY ordinal",(proposal,))
        body=row["body"]
        need([{"id":p["id"],"digest":p["digest"]} for p in rows]==body["packet_manifest"],"packet_manifest_mismatch","Local packet manifest differs")
        return row,rows

    def _plan_breakdown(self, proposal):
        """Resolve the proposal's composed root for the shared plan reader.

        The local proposal owns this selector.  The Unit 3 collector validates
        its project/program, composition digest, root status, and immutable
        body; this method only keeps the selector boundary in one place.
        """
        from .assurance_stage import _local_execution_proposed_breakdown
        return _local_execution_proposed_breakdown(
            self.c, proposal["project"], proposal["program"], proposal["id"],
        )

    def packet(self, actor, packet):
        row=self.s.one("SELECT * FROM local_execution_packets WHERE id=?",(packet,),True)
        proposal=self._proposal_row(actor,row["proposal"]);p=parse_json(row["body"],limit=MAX_PROPOSAL_BYTES)
        need(digest(p)==row["digest"] and p.get("id")==packet,"integrity_error","Local packet content differs")
        material=self._collect_material(actor,proposal,payload_from_proposal(proposal["body"]))
        need(digest(material)==proposal["body"]["material_digest"],"stale_local_execution","Local material is stale")
        serialized=canonical(material).decode()
        need(p["proposal"]==proposal["id"] and p["program"]==proposal["program"] and p.get("ordinal")==row["ordinal"] and p["material_digest"]==proposal["body"]["material_digest"]
             and p["total_characters"]==len(serialized) and p["start"]>=0 and p["end"]==p["start"]+len(p["serialized_fragment"])
             and serialized[p["start"]:p["end"]]==p["serialized_fragment"]
             and p["required_coverage"][0]=="LEXPART-"+digest([proposal["id"],proposal["body"]["material_digest"],p["start"],p["end"]]),
             "integrity_error","Local packet fragment differs from current material")
        return {**row,"body":p,"proposal_digest":proposal["digest"]}

    def get(self, actor, local_execution, offset=0, limit=50):
        need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,"invalid_range","Use a bounded local execution page")
        proposal,rows=self._packet_rows(actor,local_execution);body=proposal["body"]
        audit=self._audit(actor,local_execution,reviews=True,readonly=True)
        program=self._program(actor,proposal["program"])
        events=self.s.all("SELECT * FROM local_execution_records WHERE proposal=? ORDER BY created,id",(local_execution,))
        latest=None
        for event in events:
            latest={**event,"body":parse_json(event["body"], limit=MAX_PROPOSAL_BYTES)}
        packets=[{"id":p["id"],"digest":p["digest"],"ordinal":p["ordinal"],"bytes":len(p["body"].encode())} for p in rows[offset:offset+limit]]
        return {"id":local_execution,"project":proposal["project"],"program":proposal["program"],"subplan":proposal["subplan"],
                "digest":proposal["digest"],"material_digest":body["material_digest"],"tasks":body["tasks"],
                "packet_manifest":body["packet_manifest"],
                "latest_event":latest,"history_approved":any(e["kind"]=="certified" for e in events),
                "current":audit["current"],"current_allowed":audit["current"] and not any(e["kind"]=="withdrawn" for e in events),
                "invalidation_reasons":audit["failures"],"phase":program["phase"],"program_revision":program["revision"],
                "packet_count":len(rows),"packets":packets,"next_offset":offset+len(packets) if offset+len(packets)<len(rows) else None,
                "obligation_count":len(body.get("coverage_manifest",{}).get("obligations",[])),
                "incomplete_obligation_count":sum(1 for item in body.get("dispositions",[]) if item["disposition"]!="required_resolved"),
                "audit":{k:v for k,v in audit.items() if k not in {"material"}},
                "deploy_ready":False,"next_operation":"Run observed feasibility and impact reviews, then local_execution.certify; root phase and release gates remain mandatory."}

    def list(self, actor, program, offset=0, limit=50):
        self._program(actor,program);need(type(offset) is int and offset>=0 and type(limit) is int and 1<=limit<=200,"invalid_range","Use a bounded local execution page")
        total=self.s.one("SELECT count(*) AS n FROM local_execution_proposals WHERE program=?",(program,))["n"]
        rows=self.s.all("SELECT id,project,program,subplan,digest,created,json_extract(body,'$.material_digest') AS material_digest,json_array_length(json_extract(body,'$.tasks')) AS task_count FROM local_execution_proposals WHERE program=? ORDER BY created,id LIMIT ? OFFSET ?",(program,limit,offset))
        return {"program":program,"project":self.s.one("SELECT project FROM programs WHERE id=?",(program),True)["project"],"items":rows,"total":total,"offset":offset,"next_offset":offset+len(rows) if offset+len(rows)<total else None}

    def _audit_reviews(self, actor, proposal, rows, body, material):
        failures=[];verified=[];covered=set();covered_by_role=defaultdict(set);runs=set();impact_expected=defaultdict(set)
        marker_by_impact={"LEXIMPACT-" + digest([item["task"],item["item_id"],item["item_digest"],item["disposition"]]): item["id"]
                         for item in body["dispositions"]}
        all_required=set(body["coverage_manifest"]["stages"] + body["coverage_manifest"]["obligations"] + body["coverage_manifest"]["impact"])
        for row in rows:
            p=parse_json(row["body"])
            for marker_value in p.get("required_coverage",[]):
                if marker_value in marker_by_impact:impact_expected[row["id"]].add(marker_by_impact[marker_value])
            packet_marker=p.get("required_coverage", [None])[0]
            for role in ROLES:
                refs=self.c.g.evidence_for(row["id"],row["digest"],role)
                if not refs:
                    failures.append({"packet":row["id"],"role":role,"code":"review_required"});continue
                try:
                    ev=self.c.g.require_review(refs[0]["id"],row["id"],row["digest"],{role})
                    need(ev["run"] not in runs,"independent_review","Every local role/packet review needs a distinct observed run")
                    runs.add(ev["run"])
                    role_covered=set(ev["result"].get("covered",[]))
                    need(role_covered <= all_required | {packet_marker}, "review_coverage", "Review returned an unknown local coverage marker")
                    need(packet_marker in role_covered, "review_coverage", "This exact local packet was not covered")
                    category = "stages" if role == "feasibility" else "impact"
                    role_required=set(p.get("required_coverage", [])[1:]) & set(p.get("coverage_manifest",{}).get(category,[]))
                    # Feasibility is the shared前段 review: obligation markers
                    # must be observed by every feasibility packet review.  The
                    # impact role owns its assigned inventory markers.  The
                    # cumulative per-role check below additionally requires
                    # every semantic marker to be covered by each role.
                    if role == "feasibility":
                        role_required |= set(body["coverage_manifest"].get("obligations", []))
                    need(role_required <= role_covered, "review_coverage", "The local review role did not cover its assigned markers", role)
                    covered.update(role_covered)
                    covered_by_role[role].update(role_covered)
                    need(not ev["result"].get("findings"),"unresolved_review_findings","Local review contains unresolved findings")
                    if role=="impact":
                        expected=impact_expected[row["id"]]
                        disposition={d.get("id"):d for d in ev["result"].get("dispositions",[])}
                        for ident in expected:
                            value=disposition.get(ident)
                            need(value and value.get("resolution")=="acceptable" and isinstance(value.get("reason"),str) and value["reason"].strip(),"impact_disposition_missing","Impact review must explain every inventory disposition",ident)
                    verified.append({"packet":row["id"],"role":role,"receipt":ev["id"],"run":ev["run"],"binding":row["digest"]})
                except Fault as exc:failures.append({"packet":row["id"],"role":role,**exc.as_dict()})
        required=all_required
        # Every packet marker must also be observed; a reviewer cannot cover a
        # semantic marker by merely echoing a neighboring fragment.
        required.update("LEXPART-"+digest([proposal["id"],body["material_digest"],parse_json(row["body"])["start"],parse_json(row["body"])["end"]]) for row in rows)
        missing=sorted(required-covered)
        if missing:failures.append({"code":"review_coverage","missing":missing[:100],"missing_count":len(missing)})
        # Both managed roles inspect the complete semantic local proposal over
        # their packet runs.  Their instructions and impact dispositions remain
        # role-specific, but one role's coverage cannot discharge the other's
        # missing stage, impact or obligation marker.
        role_required={role: set(all_required) for role in ROLES}
        for role,expected in role_required.items():
            missing_role=sorted(expected-covered_by_role[role])
            if missing_role:
                failures.append({"code":"review_coverage","role":role,
                                 "missing":missing_role[:100],"missing_count":len(missing_role)})
        return failures,verified,covered,runs

    def _audit(self, actor, local_execution, reviews=True, readonly=False,
               enforce_plan=True):
        """Audit retained local material and optionally apply Unit4-P.

        The material/review checks are the readonly local qualification
        primitive shared by Unit3.  Unit4-P is a writer-boundary wrapper: it
        may call this primitive, but the primitive must not call the wrapper
        again.  ``enforce_plan=False`` is therefore reserved for that shared
        read and for the stage evaluator's local component.
        """
        proposal,rows=self._packet_rows(actor,local_execution);body=proposal["body"];failures=[];verified=[]
        try:
            material=self._collect_material(actor,proposal,{**payload_from_proposal(body),"stage_evidence":body["stage_evidence"],"dispositions":body["dispositions"]})
            need(digest(material)==body["material_digest"],"stale_local_execution","Canonical semantic material changed")
            material_digest=body["material_digest"]
            serialized=canonical(material).decode();cursor=0;fragments=[]
            need(rows and len(rows)==body["packet_count"],"packet_manifest_mismatch","Local packet count differs")
            complete_markers=set(body["coverage_manifest"]["stages"] + body["coverage_manifest"]["obligations"] + body["coverage_manifest"]["impact"])
            assigned_markers=set()
            for ordinal,row in enumerate(rows):
                p=parse_json(row["body"])
                part_marker="LEXPART-"+digest([proposal["id"],material_digest,p.get("start"),p.get("end")])
                need(row["ordinal"]==ordinal and p.get("ordinal")==ordinal and digest(p)==row["digest"] and p.get("id")==row["id"]
                     and p["proposal"]==proposal["id"] and p["material_digest"]==material_digest
                     and p["start"]==cursor and p["end"]==cursor+len(p["serialized_fragment"])
                     and p["total_characters"]==len(serialized)
                     and p["required_coverage"] and p["required_coverage"][0]==part_marker
                     and len(p["required_coverage"])==len(set(p["required_coverage"]))
                     and set(p["required_coverage"][1:]) <= complete_markers
                     and p.get("coverage_manifest")==body["coverage_manifest"]
                     and p.get("complete_manifest",{}).get("markers")==body["coverage_manifest"],
                     "integrity_error","Local packet identity or continuity differs")
                assigned_markers.update(p["required_coverage"][1:])
                cursor=p["end"];fragments.append(p["serialized_fragment"])
            need(assigned_markers==complete_markers,"review_coverage","Local packet marker manifest is incomplete")
            need(cursor==len(serialized) and digest("".join(fragments).encode())==material_digest,"missing_packet","Local material fragments are incomplete")
        except Fault as exc:failures.append(exc.as_dict());material=None
        if material is not None:
            try:
                self._audit_stage_evidence(actor,proposal["project"],body["tasks"],body["stage_evidence"],
                                           ensure_policy=not readonly)
            except Fault as exc:
                failures.append(exc.as_dict())
            # External prerequisites must already be completed/current at every
            # local gate.  Dependencies between selected Tasks are an allowed
            # local sequence (A then B) and are checked for the particular Task
            # at claim/execute/candidate time below.
            for item in material["external_tasks"]:
                dependency_row=self.s.one("SELECT status,validity FROM tasks WHERE id=?",(item["task"],))
                if not dependency_row or dependency_row["status"]!="completed" or dependency_row["validity"]!="current":
                    failures.append({"code":"external_dependency_not_current","task":item["task"]})
            disposition_by_key={(item["task"],item["item_id"]):item for item in body["dispositions"]}
            selected_tasks=body["tasks"]
            for item in material["inventory"]:
                for task in selected_tasks:
                    disposition=disposition_by_key.get((task,item["id"]))
                    if disposition is None or disposition["disposition"] == "unresolved":
                        if item["kind"] in {"source_unclassified","conflict","block"}:
                            failures.append({"code":"unresolved_inventory","item":item["id"],"task":task})
                    value=item.get("value",{})
                    if item["kind"] == "decision" and str(value.get("status","")).lower() in {"open","unanswered","unresolved"} and (disposition is None or disposition["disposition"] != "independent"):
                        failures.append({"code":"open_decision_dependency","item":item["id"],"task":task})
            if reviews:
                f,v,_,_=self._audit_reviews(actor,proposal,rows,body,material);failures.extend(f);verified.extend(v)
            else:
                failures.append({"code":"reviews_not_checked","message":"Diagnostic audit without observed reviews cannot certify"})
            # Scenario and feasibility stages need observed rationale, not only
            # an artifact name.  The scenario review must observe an accepted
            # artifact; feasibility must observe a distinct analysis Task.
            # Meaning remains the managed review's judgment.
            for task,stages in body["stage_evidence"].items():
                scenario_refs=list(stages["scenarios"].get("refs", []))
                if stages["scenarios"].get("review") is not None:
                    scenario_refs.append(stages["scenarios"]["review"])
                scenario_receipts=[ref for ref in scenario_refs if ref["kind"]=="receipt"]
                if not scenario_receipts:
                    failures.append({"code":"stage_evidence_unobserved","task":task,"stage":"scenarios"})
                else:
                    for ref in scenario_receipts:
                        try:
                            _,subject_kind=self._stage_subject_binding(actor,proposal["project"],ref["subject"],
                                                                         ensure_policy=not readonly)
                            if subject_kind != "artifact":
                                failures.append({"code":"stage_evidence_subject","task":task,"stage":"scenarios",
                                                 "receipt":ref["id"],"code_detail":"scenario_review_must_observe_artifact"})
                        except Fault as exc:
                            failures.append({"code":"stage_evidence_subject","task":task,"stage":"scenarios",
                                             "receipt":ref["id"],**exc.as_dict()})
                feasibility_refs=list(stages["feasibility"].get("refs", []))
                if stages["feasibility"].get("review") is not None:
                    feasibility_refs.append(stages["feasibility"]["review"])
                feasibility_receipts=[ref for ref in feasibility_refs if ref["kind"]=="receipt"]
                if not feasibility_receipts:
                    failures.append({"code":"stage_evidence_unobserved","task":task,"stage":"feasibility"})
                else:
                    analysis_receipts=[]
                    for ref in feasibility_receipts:
                        try:
                            subject_row=self.s.one("SELECT body,status,validity FROM tasks WHERE id=?",(ref["subject"],))
                            if (subject_row and parse_json(subject_row["body"]).get("task_kind")=="analysis"
                                    and subject_row["status"]=="completed" and subject_row["validity"]=="current"):
                                analysis_receipts.append(ref)
                        except Fault:
                            pass
                    if not analysis_receipts:
                        failures.append({"code":"stage_evidence_subject","task":task,"stage":"feasibility",
                                         "code_detail":"feasibility_review_must_observe_analysis_task"})
            if any(d["disposition"]=="unresolved" for d in body["dispositions"]):failures.append({"code":"unresolved_disposition"})
            subplan_report=self.c.subplans.audit(actor,body["subplan"],reviews=reviews,
                                                 readonly=readonly)
            if not subplan_report["current"]:failures.append({"code":"subplan_not_current","failures":subplan_report["failures"][:100]})
            if reviews:
                local_runs={item["run"] for item in verified}
                for item in subplan_report.get("reviewed",[]):
                    try:
                        subplan_receipt=self.c.g.receipt(item["receipt"])
                        if subplan_receipt["run"] in local_runs:
                            failures.append({"code":"independent_review","run":subplan_receipt["run"],"message":"Local and subplan reviews must use distinct observed runs"})
                    except Fault as exc:
                        failures.append({"code":"review_reference_invalid","receipt":item.get("receipt"),**exc.as_dict()})
            # A selected Task must remain canonical/current and production.  Its
            # status is intentionally dynamic and is checked again before claim.
            for task in body["tasks"]:
                row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,))
                if not row or row["project"]!=proposal["project"] or row["status"]=="cancelled" or row["validity"]!="current":failures.append({"code":"selected_task_invalid","task":task})
                else:
                    definition=self._task_definition(actor,task)
                    if definition["body"].get("task_kind")!="production":failures.append({"code":"selected_task_not_production","task":task})
                    project_state=self.s.one("SELECT paused FROM projects WHERE id=?",(proposal["project"],),True)
                    if project_state["paused"]:failures.append({"code":"project_paused","task":task})
                    if row["paused"]:failures.append({"code":"task_paused","task":task})
                    for issue in self.c.g.check_current(task, ensure_policy=not readonly):
                        failures.append({"code":"selected_task_dynamic_failure","task":task,"detail":issue})
        # The local packet/review audit is one half of the certification gate.
        # The Unit4-P plan proof is a caller-owned wrapper.  The stage
        # evaluator calls this method with ``enforce_plan=False`` so the
        # readonly primitive cannot recurse through the writer boundary.
        plan_gate=None
        if enforce_plan:
            try:
                from .unit4_enforcement import inspect_plan_gate
                plan_gate=inspect_plan_gate(
                    self.c, actor, project=proposal["project"], program=proposal["program"],
                    proposed_breakdown=self._plan_breakdown(proposal),
                    local_execution=local_execution,
                )
            except Fault as exc:
                plan_gate={
                    "format":"daikibo.unit4-plan-gate.v1", "allowed":False,
                    "required":True, "reason":exc.code, "origin":None,
                    "selection":None, "stage":"plan", "checkpoint":"plan",
                    "evaluation":None, "semantic_fingerprint":None,
                    "report_snapshot":None,
                    "failures":[{"code":exc.code,"reason":str(exc),"status":"unknown"}],
                }
            if plan_gate.get("allowed") is not True:
                failures.append({"code":"stage_assurance_blocked",
                                 "reason":plan_gate.get("reason","plan gate denied"),
                                 "plan_gate":plan_gate})
        return {"local_execution":local_execution,"project":proposal["project"],"program":proposal["program"],"material_digest":body["material_digest"],
                "current":not failures,"failures":failures,"reviewed":verified,"review_checks_performed":reviews,
                "plan_gate":plan_gate,
                "packet_count":len(rows),"material":material,"deploy_ready":False,"semantic_correctness_guaranteed":False}

    def audit(self, actor, local_execution, reviews=True, offset=0, limit=100):
        need(type(reviews)is bool,"invalid_option","reviews must be boolean")
        need(type(offset)is int and offset>=0 and type(limit)is int and 1<=limit<=500,"invalid_range","Use bounded audit pages")
        report=self._audit(actor,local_execution,reviews,readonly=True)
        failures=report["failures"];reviewed=report["reviewed"]
        return {**report,"material":None,"failures":failures[offset:offset+limit],"reviewed":reviewed[offset:offset+limit],
                "failure_count":len(failures),"reviewed_count":len(reviewed),
                "next_failure_offset":offset+limit if offset+limit<len(failures) else None,
                "next_review_offset":offset+limit if offset+limit<len(reviewed) else None}

    def _event(self, actor, proposal, kind, body, task=None, epoch=None):
        ident=uid("LEXREC");value={"format":"daikibo.local-execution-record.v1","proposal":proposal,"kind":kind,
                                  "task":task,"epoch":epoch,**body,"created_by":actor.id}
        h=digest(value)
        self.s.execute("INSERT INTO local_execution_records VALUES(?,?,?,?,?,?,?,?,?)",
                       (ident,self.s.one("SELECT project FROM local_execution_proposals WHERE id=?",(proposal,),True)["project"],proposal,task,epoch,kind,h,canonical(value).decode(),timestamp()))
        return {"id":ident,"proposal":proposal,"task":task,"epoch":epoch,"kind":kind,"digest":h,"body":value}

    def certify(self, actor, local_execution, expected_digest, request_id=None):
        proposal=self._proposal_row(actor,local_execution);actor.require("owner","agent",project=proposal["project"])
        need(proposal["digest"]==expected_digest,"stale_digest","Local execution proposal changed")
        if request_id is not None:text(request_id,"request_id",256)
        with self.s.transaction():
            withdrawn=self.s.one("SELECT id FROM local_execution_records WHERE proposal=? AND kind='withdrawn'",(local_execution,))
            need(not withdrawn,"local_execution_withdrawn","Withdrawn local execution proposals cannot be recertified")
            # Audit and append are one serialized transition.  In particular,
            # an old request ID cannot bypass a review failure introduced after
            # its first successful certification.
            report=self._audit(actor,local_execution,True)
            need(report["current"],"local_execution_gate_denied","Observed local execution reviews are incomplete",report["failures"])
            from .unit4_enforcement import require_plan_gate
            plan_gate=require_plan_gate(
                self.c,actor,project=proposal["project"],program=proposal["program"],
                proposed_breakdown=self._plan_breakdown(proposal),
                local_execution=local_execution,
            )
            reviews=report["reviewed"];review_digest=digest(reviews)
            existing=self.s.one("SELECT * FROM local_execution_records WHERE proposal=? AND kind='certified' ORDER BY created DESC LIMIT 1",(local_execution,))
            if existing:
                old=parse_json(existing["body"], limit=MAX_PROPOSAL_BYTES)
                need(old.get("material_digest")==proposal["body"]["material_digest"],"stale_local_execution","Current certification material differs")
                need(old.get("review_digest")==review_digest,"local_execution_already_certified","A different observed review set requires a new local proposal")
                if request_id is None or old.get("request_id")==request_id:
                    return {"id":existing["id"],"digest":existing["digest"],"replayed":True,"current":True,
                            "plan_gate":plan_gate}
                return {"id":existing["id"],"digest":existing["digest"],"replayed":True,"current":True,
                        "plan_gate":plan_gate}
            body={"format":"daikibo.local-execution-certification.v1","proposal":local_execution,"proposal_digest":proposal["digest"],
                  "material_digest":proposal["body"]["material_digest"],"reviews":reviews,"review_digest":review_digest,"request_id":request_id,
                  "packet_manifest":proposal["body"]["packet_manifest"],"tasks":proposal["body"]["tasks"],
                  "plan_gate":plan_gate,"current":True,"deploy_ready":False}
            event=self._event(actor,local_execution,"certified",body)
            self.c.sec.event(proposal["project"],"local_execution_certified",actor.id,{"proposal":local_execution,"record":event["id"],"material_digest":body["material_digest"],"tasks":body["tasks"],"plan_proof":plan_gate.get("proof_digest")})
        return {"id":event["id"],"proposal":local_execution,"digest":event["digest"],"material_digest":body["material_digest"],"replayed":False,"current":True,"tasks":body["tasks"],"plan_gate":plan_gate,"deploy_ready":False}

    def withdraw(self, actor, local_execution, expected_digest, reason, request_id=None):
        proposal=self._proposal_row(actor,local_execution);actor.require("owner","agent",project=proposal["project"])
        need(proposal["digest"]==expected_digest,"stale_digest","Local execution proposal changed");text(reason,"withdrawal reason",12000)
        if request_id is not None:text(request_id,"request_id",256)
        old=self.s.one("SELECT * FROM local_execution_records WHERE proposal=? AND kind='withdrawn' ORDER BY created DESC LIMIT 1",(local_execution,))
        if old:
            value=parse_json(old["body"], limit=MAX_PROPOSAL_BYTES);need(value.get("reason")==reason,"idempotency_conflict","Withdrawal was already recorded with another reason")
            return {"id":old["id"],"proposal":local_execution,"replayed":True,"withdrawn":True}
        with self.s.transaction():
            event=self._event(actor,local_execution,"withdrawn",{"format":"daikibo.local-execution-withdrawal.v1","proposal_digest":expected_digest,"material_digest":proposal["body"]["material_digest"],"reason":reason,"request_id":request_id})
            self.c.sec.event(proposal["project"],"local_execution_withdrawn",actor.id,{"proposal":local_execution,"record":event["id"],"reason":reason})
        return {"id":event["id"],"proposal":local_execution,"replayed":False,"withdrawn":True,"tasks":proposal["body"]["tasks"]}

    # ---------- workflow integration ----------

    def _proposals_for_task(self, task):
        rows=self.s.all("SELECT * FROM local_execution_proposals WHERE json_array_length(json_extract(body,'$.tasks'))>0 ORDER BY created DESC,id DESC")
        result=[]
        for row in rows:
            body=parse_json(row["body"], limit=MAX_PROPOSAL_BYTES)
            if task in body.get("tasks",[]):result.append(row|{"body":body})
        return result

    def _proposal_has_negative_series(self, proposal):
        """Detect an explicit newer negative review/event for one proposal.

        An unfinished draft is neutral: it must not revoke an already current
        certification merely because it was created later.  A withdrawal,
        invalidation, or observed latest failing review is an explicit negative
        series event and therefore blocks fallback to an older certification
        with the same Task/material key.
        """
        if self.s.one("SELECT id FROM local_execution_records WHERE proposal=? AND kind IN ('withdrawn','invalidated') LIMIT 1", (proposal["id"],)):
            return True
        packets=self.s.all("SELECT id,digest FROM local_execution_packets WHERE proposal=? ORDER BY ordinal", (proposal["id"],))
        for packet in packets:
            for role in ROLES:
                refs=self.c.g.evidence_for(packet["id"],packet["digest"],role)
                if not refs:
                    continue
                try:
                    evidence=self.c.g.receipt(refs[0]["id"])
                except Fault:
                    return True
                result=evidence.get("result",{})
                if (result.get("verdict")!="pass" or result.get("findings")
                        or evidence.get("exit_code")!=0
                        or any(evidence.get(flag) for flag in ("timed_out","cancelled","output_overflow","failure"))):
                    return True
        return False

    def _selected_dependency_failures(self, actor, task, proposal_body):
        """Check only the selected Task's internal prerequisites at runtime."""
        selected=set(proposal_body.get("tasks", []));failures=[]
        definition=self._task_definition(actor,task)
        for dependency in definition["dependencies"]:
            if dependency not in selected:
                # The complete external closure was checked by _audit.  Keep
                # this branch explicit so a malformed proposal cannot turn an
                # omitted prerequisite into an implicit allowance.
                dependency_row=self.s.one("SELECT status,validity FROM tasks WHERE id=?",(dependency,))
                if not dependency_row or dependency_row["status"]!="completed" or dependency_row["validity"]!="current":
                    failures.append({"code":"external_dependency_not_current","task":task,"dependency":dependency})
            else:
                dependency_row=self.s.one("SELECT status,validity FROM tasks WHERE id=?",(dependency,))
                if not dependency_row or dependency_row["status"]!="completed" or dependency_row["validity"]!="current":
                    failures.append({"code":"selected_dependency_not_current","task":task,"dependency":dependency})
        return failures

    def current_authorization_readonly(self, actor, task, stage="ready", pinned=None):
        """Resolve current local authority without invoking a stage writer.

        This is the shared read primitive for the workflow/runtime wrappers and
        the private candidate provenance boundary.  It only rereads the
        retained proposal, certification, Task assignment/definition and
        current reviews; it never creates a gate, claim, candidate, or stage
        result.  Keep the stage argument as an input to the same finite
        currentness contract even while later checkpoint-specific enforcement
        remains outside this unit.
        """
        need(stage in {"ready", "claim", "execute", "candidate", "complete", "recheck"},
             "invalid_stage", "Unknown local authorization stage")
        row=self.s.one("SELECT * FROM tasks WHERE id=?",(task,),True);self.c.k.project(actor,row["project"])
        proposals=self._proposals_for_task(task)
        if not proposals:return None
        proposals=[p for p in proposals if p["project"]==row["project"]]
        if not proposals:return None
        # A later draft is neutral until it has an explicit negative review or
        # event.  Select the newest certified proposal, then prevent that
        # certification from being resurrected across a newer negative series
        # for the same Task/material key.
        certified_candidates=[]
        for candidate in proposals:
            certified=self.s.one("SELECT * FROM local_execution_records WHERE proposal=? AND kind='certified' ORDER BY created DESC,id DESC LIMIT 1",(candidate["id"],))
            if certified and not self.s.one("SELECT id FROM local_execution_records WHERE proposal=? AND kind='withdrawn' LIMIT 1",(candidate["id"],)):
                certified_candidates.append((candidate,certified))
        if not certified_candidates:
            newest=proposals[0]
            return {"allowed":False,"failures":["local_execution_withdrawn"] if self.s.one("SELECT id FROM local_execution_records WHERE proposal=? AND kind='withdrawn'",(newest["id"],)) else ["local_execution_not_certified"],"proposal":newest["id"]}
        proposal,certified=certified_candidates[0]
        material_key=proposal["body"].get("material_digest")
        selected_index=proposals.index(proposal)
        for newer in proposals[:selected_index]:
            if newer["body"].get("material_digest")==material_key and self._proposal_has_negative_series(newer):
                return {"allowed":False,"failures":["local_execution_superseded"],"proposal":newer["id"],"certification":certified["id"]}
        report=self._audit(actor,proposal["id"],True,readonly=True,
                           enforce_plan=False)
        report["failures"].extend(self._selected_dependency_failures(actor,task,proposal["body"]))
        report["current"]=not report["failures"]
        if not report["current"]:return {"allowed":False,"failures":["local_execution_stale",*[_failure_code(x) for x in report["failures"]]],"proposal":proposal["id"],"certification":certified["id"]}
        event_body=parse_json(certified["body"], limit=MAX_PROPOSAL_BYTES)
        if pinned is not None and (pinned.get("id")!=certified["id"] or pinned.get("digest")!=certified["digest"]):
            return {"allowed":False,"failures":["local_execution_authorization_changed"],"proposal":proposal["id"],"certification":certified["id"]}
        return {"allowed":True,"route":"local","proposal":proposal["id"],"material_digest":proposal["body"]["material_digest"],
                "certification":{"id":certified["id"],"digest":certified["digest"]},"tasks":proposal["body"]["tasks"],"failures":[],"event":event_body}

    def current_authorization(self, actor, task, stage="ready", pinned=None):
        """Compatibility wrapper for the existing public local route."""
        return self.current_authorization_readonly(actor, task, stage, pinned)

    def _write_claim(self, actor, task, epoch, auth):
        old=self.s.one("SELECT * FROM local_execution_records WHERE proposal=? AND task=? AND epoch=? AND kind='claimed'",(auth["proposal"],task,epoch))
        if old:return {"id":old["id"],"digest":old["digest"],"replayed":True,"authorization":auth["certification"]}
        body={"format":"daikibo.local-execution-claim.v1","proposal":auth["proposal"],"certified_event":auth["certification"],"certification_digest":auth["certification"]["digest"],"material_digest":auth["material_digest"],"task":task,"epoch":epoch,"task_semantic_digest":self._task_semantic_digest(self._task_definition(actor,task))}
        event=self._event(actor,auth["proposal"],"claimed",body,task,epoch)
        return {"id":event["id"],"digest":event["digest"],"replayed":False,"authorization":auth["certification"]}

    def claim(self, actor, task, epoch):
        row=self.s.one("SELECT status,epoch FROM tasks WHERE id=?",(task,),True)
        need(row["status"]=="running" and row["epoch"]==epoch,"stale_claim","Local claim must bind the currently running Task epoch")
        auth=self.current_authorization(actor,task,"claim");need(auth and auth.get("allowed"),"local_execution_not_ready","Current local execution authorization is not valid",auth)
        return self._write_claim(actor,task,epoch,auth)

    def _claim_from_task_admission(self, actor, task, epoch, binding):
        """Persist the local branch already selected by canonical Task admission.

        This private writer is called only inside Workflow.claim's transaction.
        It re-resolves current authorization there and pins the exact proposal,
        certification, material, and owner that the readonly admission chose.
        """
        from .unit4_enforcement import LocalClaimBinding

        need(isinstance(binding, LocalClaimBinding) and binding.task == task,
             "invalid_local_claim_binding", "Local claim binding does not name this Task")
        row=self.s.one("SELECT status,epoch,project FROM tasks WHERE id=?",(task,),True)
        need(row["status"]=="running" and row["epoch"]==epoch,
             "stale_claim","Local claim must bind the currently running Task epoch")
        auth=self.current_authorization_readonly(actor,task,"claim")
        need(auth and auth.get("allowed"),
             "local_execution_not_ready","Current local execution authorization is not valid",auth)
        need(auth.get("proposal")==binding.proposal and
             auth.get("material_digest")==binding.material_digest and
             auth.get("certification")=={"id":binding.certification_id,
                                          "digest":binding.certification_digest},
             "local_execution_authorization_changed",
             "Current local authorization differs from Task admission",auth)
        proposal=self.s.one(
            "SELECT project,program FROM local_execution_proposals WHERE id=?",
            (binding.proposal,),True,
        )
        need(proposal["project"]==row["project"] and proposal["program"]==binding.program,
             "local_execution_authorization_changed",
             "Current local proposal owner differs from Task admission",binding.proposal)
        return self._write_claim(actor,task,epoch,auth)

    def claimed(self, task, epoch=None):
        if epoch is None:
            row=self.s.one("SELECT epoch FROM tasks WHERE id=?",(task,),True);epoch=row["epoch"]
        rows=self.s.all("SELECT * FROM local_execution_records WHERE task=? AND epoch=? AND kind='claimed' ORDER BY created DESC,id DESC",(task,epoch))
        return rows[0] if rows else None

    def review_subject(self, actor, packet, role):
        """Return the standard managed-review tuple for a local packet."""
        need(role in ROLES, "invalid_role", "Local execution packets require feasibility and impact review")
        row=self.packet(actor,packet)
        body=row["body"]
        context={**body,
                 "complete_material_digest":body["material_digest"],
                 "required_coverage":body["required_coverage"],
                 "complete_manifest":body.get("complete_manifest",{}),
                 "review_role":role,
                 "instructions":body.get("instructions","")}
        return row["project"],row["digest"],_empty_snapshot(),context,None

    def execution_readiness(self, actor, task, stage="ready", pinned=None):
        """Shared dynamic local authorization check used by workflow/runtime."""
        need(stage in {"ready", "claim", "execute", "candidate", "complete", "recheck"},
             "invalid_stage", "Unknown execution readiness stage")
        return self.current_authorization(actor, task, stage, pinned)


def payload_from_proposal(body):
    return {"program":body["program"],"subplan":body["subplan"],"tasks":body["tasks"],"rationale":body["rationale"],
            "stage_evidence":body["stage_evidence"],"dispositions":body["dispositions"]}


def _failure_code(value):
    return value.get("code","unknown") if isinstance(value,dict) else "unknown"
