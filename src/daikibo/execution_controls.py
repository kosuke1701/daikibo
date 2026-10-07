"""Version-bound execution duration and attempt-assessment controls.

This module owns the execution-control records introduced by dev18.  It keeps
the legacy ``tasks.attempts`` value as telemetry, records every new claim, and
lets only an observed independent reviewer classify a terminal attempt.  An
inconclusive review remains an ordinary receipt and can be followed by a later
decisive review; it never spends the one finalized assessment slot.
"""
from __future__ import annotations

import math
import hashlib
import time

from .common import Fault, canonical, digest, finite_duration, need, number, obj, parse_json, text, timestamp, uid


DEFAULT_TASK_TIMEOUT_SECONDS = 14_400.0
MAX_NO_PROGRESS_ATTEMPTS = 3
PACKET_BYTES = 180_000
PROGRESS_REF_LIMIT = 16


SCHEMA = r'''
ALTER TABLE tasks ADD COLUMN no_progress_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS execution_attempts(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 attempt_epoch INTEGER NOT NULL, attempt_ordinal INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 task_binding TEXT NOT NULL, status TEXT NOT NULL,
 implementer_run TEXT REFERENCES runs(id), implementer_receipt TEXT REFERENCES receipts(id),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(task,attempt_epoch), UNIQUE(task,attempt_ordinal)
);
CREATE INDEX IF NOT EXISTS execution_attempts_project ON execution_attempts(project,task,attempt_ordinal);

CREATE TABLE IF NOT EXISTS attempt_assessments(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 attempt_epoch INTEGER NOT NULL, attempt_ordinal INTEGER, task_revision INTEGER,
 task_binding TEXT NOT NULL, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 proposal_digest TEXT NOT NULL, implementer_run TEXT NOT NULL, implementer_receipt TEXT NOT NULL,
 reviewer_run TEXT NOT NULL, reviewer_receipt TEXT NOT NULL,
 judgment TEXT NOT NULL CHECK(judgment IN ('progress','no_progress')),
 rationale TEXT NOT NULL, evidence TEXT NOT NULL CHECK(json_valid(evidence)),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(task,attempt_epoch)
);
CREATE INDEX IF NOT EXISTS attempt_assessments_task ON attempt_assessments(task,attempt_ordinal,created);

CREATE TABLE IF NOT EXISTS execution_control_proposals(
 id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), project TEXT NOT NULL REFERENCES projects(id),
 task_revision INTEGER NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL,
 binding TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('proposed','applied','withdrawn','superseded')),
 result TEXT CHECK(result IS NULL OR json_valid(result)), created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_proposals_task ON execution_control_proposals(task,created,id);

CREATE TABLE IF NOT EXISTS execution_control_packets(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(proposal,ordinal)
);
CREATE INDEX IF NOT EXISTS execution_control_packets_proposal ON execution_control_packets(proposal,ordinal);

CREATE TABLE IF NOT EXISTS execution_control_events(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL
 CHECK(kind IN ('applied','withdrawn','superseded')),
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_events_proposal ON execution_control_events(proposal,created,id);

CREATE TABLE IF NOT EXISTS execution_control_authorizations(
 id TEXT PRIMARY KEY, proposal TEXT NOT NULL REFERENCES execution_control_proposals(id),
 project TEXT NOT NULL REFERENCES projects(id), task TEXT NOT NULL REFERENCES tasks(id),
 task_revision INTEGER NOT NULL, control_revision INTEGER NOT NULL, proposal_digest TEXT NOT NULL,
 requested_seconds REAL, effective_seconds REAL, assessment TEXT
 CHECK(assessment IS NULL OR assessment IN ('progress','no_progress')),
 reviewer_run TEXT NOT NULL, reviewer_receipt TEXT NOT NULL,
 body TEXT NOT NULL CHECK(json_valid(body)), digest TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_control_authorizations_task ON execution_control_authorizations(task,created,id);
CREATE UNIQUE INDEX IF NOT EXISTS execution_control_authorizations_proposal ON execution_control_authorizations(proposal);

CREATE TRIGGER IF NOT EXISTS execution_attempts_identity_immutable
 BEFORE UPDATE OF task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,body,digest,created
 ON execution_attempts BEGIN SELECT RAISE(ABORT,'immutable execution attempt identity'); END;
CREATE TRIGGER IF NOT EXISTS execution_attempts_no_delete
 BEFORE DELETE ON execution_attempts BEGIN SELECT RAISE(ABORT,'retain execution attempt history'); END;
CREATE TRIGGER IF NOT EXISTS attempt_assessments_immutable
 BEFORE UPDATE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'immutable attempt assessment'); END;
CREATE TRIGGER IF NOT EXISTS attempt_assessments_no_delete
 BEFORE DELETE ON attempt_assessments BEGIN SELECT RAISE(ABORT,'retain attempt assessment history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_proposals_identity_immutable
 BEFORE UPDATE OF task,project,task_revision,body,digest,binding,created
 ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'immutable execution control proposal'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_proposals_no_delete
 BEFORE DELETE ON execution_control_proposals BEGIN SELECT RAISE(ABORT,'retain execution control proposal history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_packets_immutable
 BEFORE UPDATE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'immutable execution control packet'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_packets_no_delete
 BEFORE DELETE ON execution_control_packets BEGIN SELECT RAISE(ABORT,'retain execution control packet history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_events_immutable
 BEFORE UPDATE ON execution_control_events BEGIN SELECT RAISE(ABORT,'immutable execution control event'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_events_no_delete
 BEFORE DELETE ON execution_control_events BEGIN SELECT RAISE(ABORT,'retain execution control event history'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_authorizations_immutable
 BEFORE UPDATE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'immutable execution control authorization'); END;
CREATE TRIGGER IF NOT EXISTS execution_control_authorizations_no_delete
 BEFORE DELETE ON execution_control_authorizations BEGIN SELECT RAISE(ABORT,'retain execution control authorization history'); END;
'''


def _require_duration(value, name):
    return finite_duration(value, name)


class ExecutionControls:
    """Core execution-control service attached to :class:`daikibo.control.Control`."""

    def __init__(self, control):
        self.c = control
        self.s = control.s
        self.k = control.k
        self.g = control.g
        self.sec = control.sec

    # ---------- immutable identity and policy helpers ----------

    def _task(self, actor, task):
        row = self.s.one("SELECT * FROM tasks WHERE id=?", (task,), True)
        self.k.project(actor, row["project"])
        row["body"] = parse_json(row["body"])
        return row

    def _policy(self, project, readonly=False):
        row = self.g.policy(project, create=not readonly)
        body = row["body"]
        # Existing schema-12 policies remain byte-for-byte untouched.  These
        # defaults are the effective v2 values until the reviewed policy
        # adoption route records the explicit new body.
        return row, {
            **body,
            "default_task_timeout_seconds": body.get("default_task_timeout_seconds", DEFAULT_TASK_TIMEOUT_SECONDS),
            "max_no_progress_attempts": MAX_NO_PROGRESS_ATTEMPTS,
        }

    def _threshold(self, row, readonly=False):
        _policy, policy = self._policy(row["project"], readonly=readonly)
        # The accepted dev18 policy has one independently reviewed threshold.
        # Do not add a second per-task policy knob.
        return MAX_NO_PROGRESS_ATTEMPTS

    # ---------- source-backed policy v2 adoption ----------

    def _requirement_review(self, requirement):
        """Find the retained observed review which accepted the requirement.

        The source-backed policy route must be grounded in the same accepted
        requirement review that made the requirement usable.  An accepted
        status alone is a database label; retain and verify the receipt named
        by the corresponding artifact-accepted event.
        """
        for event in self.s.all("SELECT body FROM events WHERE project=? AND kind='artifact_accepted' ORDER BY seq DESC",
                                (requirement["project"],)):
            value = parse_json(event["body"])
            if value.get("id") != requirement["id"] or value.get("revision") != requirement["revision"] \
                    or value.get("digest") != requirement["digest"] or not value.get("review"):
                continue
            try:
                review = self.g.receipt(value["review"])
                raw = self.s.blob_get(review["input_digest"])
                prompt = parse_json(raw)
                need(isinstance(prompt, dict) and isinstance(prompt.get("context"), dict),
                     "invalid_evidence", "Requirement review prompt has no retained context")
                artifact = prompt["context"].get("artifact", {})
                need(isinstance(artifact, dict), "invalid_evidence",
                     "Requirement review prompt has no retained artifact")
                # This proves the material of the historical acceptance,
                # rather than comparing it with today's policy/invariants.
                # Both legacy body bindings and canonical material bindings
                # retain the exact reviewed artifact in their saved prompt.
                retained = {key: artifact.get(key) for key in
                            ("id", "project", "kind", "revision", "digest", "body")}
                expected = {key: requirement[key] for key in
                            ("id", "project", "kind", "revision", "digest")}
                expected["body"] = parse_json(requirement["body"])
                material_matches = (retained == expected and
                                    digest(raw) == review["input_digest"] and
                                    prompt.get("subject") == review.get("subject") and
                                    prompt.get("role") == review.get("role") and
                                    prompt.get("binding") == review.get("binding"))
            except Fault:
                continue
            if (review.get("subject") == requirement["id"]
                    and material_matches
                    and review.get("role") in {"requirements", "design", "consistency"}
                    and review.get("exit_code") == 0
                    and review.get("result", {}).get("verdict") == "pass"
                    and review.get("readonly_verified") is True
                    and review.get("judgment_valid") is True
                    and not any(review.get(key) for key in ("timed_out", "cancelled", "output_overflow", "failure"))):
                return value["review"]
        raise Fault("review_required", "The accepted requirement has no retained observed requirements review")

    def _policy_refs(self, actor, project, body):
        obj(body, required=("source", "requirement", "expected_policy", "supersedes", "reason"), optional=(), name="policy adoption")
        obj(body["source"], required=("id", "digest"), optional=(), name="policy source")
        obj(body["requirement"], required=("id", "revision", "digest"), optional=(), name="policy requirement")
        obj(body["expected_policy"], required=("revision", "digest"), optional=(), name="expected policy")
        text(body["reason"], "reason", 40_000)
        need(isinstance(body["supersedes"], list), "invalid_policy", "supersedes must be a list")
        for value in body["supersedes"]:
            obj(value, required=("id", "digest"), optional=(), name="superseded policy")
        source = self.s.one("SELECT * FROM sources WHERE id=?", (body["source"]["id"],), True)
        need(source["project"] == project and source["trust"] == "human", "human_input_required",
             "Policy adoption needs an authenticated human source")
        need(source["blob"] == body["source"]["digest"], "stale_source", "Policy source digest differs")
        self.s.blob_get(source["blob"])
        requirement = self.s.one("SELECT * FROM artifacts WHERE id=?", (body["requirement"]["id"],), True)
        need(requirement["project"] == project and requirement["kind"] == "requirement" and requirement["status"] == "accepted",
             "invalid_requirement", "Policy requirement must be an accepted project requirement")
        need(requirement["revision"] == body["requirement"]["revision"] and requirement["digest"] == body["requirement"]["digest"],
             "stale_requirement", "Policy requirement revision or digest differs")
        requirement_body = parse_json(requirement["body"])
        need(isinstance(requirement_body.get("source_refs"), list)
             and all(isinstance(value, str) for value in requirement_body["source_refs"])
             and body["source"]["id"] in requirement_body["source_refs"],
             "ungrounded_requirement", "Accepted requirement does not cite the human source")
        # The requirement review establishes that this accepted artifact is a
        # valid source-grounded requirement.  The exact policy diff receives
        # its own consistency review after the immutable adoption proposal is
        # recorded; no lexical source match is treated as approval.
        requirement_review = self._requirement_review(requirement)
        old = self.g.policy(project)
        need(body["expected_policy"]["revision"] == old["revision"] and body["expected_policy"]["digest"] == old["digest"],
             "stale_policy", "Expected policy is not current")
        superseded = []
        for value in body["supersedes"]:
            decision = self.s.one("SELECT * FROM decisions WHERE id=? AND project=?", (value["id"], project), True)
            decision_body = parse_json(decision["body"])
            need(decision["digest"] == value["digest"] and decision_body.get("type") == "policy"
                 and decision["status"] in {"pending", "provisional", "deferred", "decision_received"},
                 "stale_policy", "Superseded policy decision is not a reviewed pending record")
            superseded.append({"id": decision["id"], "digest": decision["digest"], "status": decision["status"],
                               "body": decision_body, "source": decision["source"], "response": decision["response"]})
        return source, requirement, requirement_review, old, superseded

    def policy_propose(self, actor, project, body):
        actor.require("owner", "agent", project=project)
        self.k.project(actor, project)
        source, requirement, requirement_review, old, superseded = self._policy_refs(actor, project, body)
        new = dict(old["body"])
        new["version"] = 2
        new["default_task_timeout_seconds"] = int(DEFAULT_TASK_TIMEOUT_SECONDS)
        new["max_no_progress_attempts"] = MAX_NO_PROGRESS_ATTEMPTS
        new["max_run_seconds"] = max(int(new.get("max_run_seconds", DEFAULT_TASK_TIMEOUT_SECONDS)), int(DEFAULT_TASK_TIMEOUT_SECONDS))
        changes = []
        for field in ("version", "default_task_timeout_seconds", "max_no_progress_attempts", "max_run_seconds"):
            before = old["body"].get(field)
            after = new.get(field)
            if before != after:
                changes.append({"field": field, "before": before, "after": after})
        changes.sort(key=lambda value: value["field"])
        payload = {"type": "execution_control_policy", "format": "daikibo.execution-policy.v1",
                   "source": {"id": source["id"], "digest": source["blob"]},
                   "requirement": {"id": requirement["id"], "revision": requirement["revision"], "digest": requirement["digest"]},
                   "old_revision": old["revision"], "old_digest": old["digest"], "old_body": old["body"],
                   "body": new, "changes": changes,
                   "supersedes": [{"id": value["id"], "digest": value["digest"], "status": value["status"]}
                                  for value in superseded],
                   "reason": body["reason"]}
        ident = uid("POLICY-PROPOSAL")
        h = digest(payload)
        with self.s.transaction():
            self.s.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (ident, project, 1, canonical(payload).decode(), h, "instruction_recorded", None,
                            source["id"], None, timestamp()))
            self.g.inbox(project, "product_decision", ident, payload, "critical")
            self.sec.event(project, "execution_policy_recorded", actor.id,
                           {"decision": ident, "digest": h, "source": source["id"],
                            "requirement": requirement["id"], "requirement_review": requirement_review})
        return {"id": ident, "revision": 1, "digest": h, "status": "instruction_recorded", "body": payload}

    def policy_binding(self, proposal):
        row = self.s.one("SELECT * FROM decisions WHERE id=?", (proposal,), True)
        body = parse_json(row["body"])
        need(body.get("type") == "execution_control_policy" and row["status"] == "instruction_recorded",
             "invalid_state", "Policy proposal is not awaiting consistency review")
        source = self.s.one("SELECT id,project,blob,trust FROM sources WHERE id=?", (body["source"]["id"],), True)
        requirement = self.s.one("SELECT id,project,revision,digest,status FROM artifacts WHERE id=?", (body["requirement"]["id"],), True)
        supersedes = []
        for value in body.get("supersedes", []):
            decision = self.s.one("SELECT id,project,body,digest,status,response,source FROM decisions WHERE id=? AND project=?",
                                  (value["id"], row["project"]), True)
            frozen_status = value.get("status")
            need(decision["digest"] == value["digest"] and decision["status"] == frozen_status,
                 "stale_policy", "A superseded pending policy decision changed")
            need(parse_json(decision["body"]).get("type") == "policy",
                 "stale_policy", "Superseded decision is not a policy record")
            supersedes.append({"id": decision["id"], "digest": decision["digest"],
                               "status": decision["status"], "response": decision["response"],
                               "source": decision["source"], "body": parse_json(decision["body"])})
        current = self.g.policy(row["project"])
        return digest({"decision": proposal, "body_digest": row["digest"], "source": source,
                       "requirement": requirement, "current_policy": {"revision": current["revision"], "digest": current["digest"]},
                       "supersedes": supersedes,
                       "accepted_artifacts": self.s.all("SELECT id,revision,digest FROM artifacts WHERE project=? AND status='accepted' ORDER BY id", (row["project"],))})

    def policy_review_subject(self, actor, proposal, role):
        need(role == "consistency", "invalid_role", "Policy adoption requires consistency review")
        row = self.s.one("SELECT * FROM decisions WHERE id=?", (proposal,), True)
        self.k.project(actor, row["project"])
        body = parse_json(row["body"])
        binding = self.policy_binding(proposal)
        source = self.s.one("SELECT * FROM sources WHERE id=?", (body["source"]["id"],), True)
        requirement = self.s.one("SELECT * FROM artifacts WHERE id=?", (body["requirement"]["id"],), True)
        source_text = self.s.blob_get(source["blob"]).decode()
        requirement_review = self._requirement_review(requirement)
        superseded = []
        for value in body.get("supersedes", []):
            decision = self.s.one("SELECT * FROM decisions WHERE id=? AND project=?", (value["id"], row["project"]), True)
            need(decision["digest"] == value["digest"] and decision["status"] == value.get("status"),
                 "stale_policy", "A superseded pending policy decision changed")
            inbox = self.s.one("SELECT id,status,body FROM inbox WHERE project=? AND ref=?", (row["project"], value["id"]))
            superseded.append({"decision": decision["id"], "digest": decision["digest"],
                               "status": decision["status"], "response": decision["response"],
                               "source": decision["source"], "body": parse_json(decision["body"]),
                               "inbox": {"id": inbox["id"], "status": inbox["status"],
                                         "body": parse_json(inbox["body"])} if inbox else None})
        context = {"policy_proposal": body, "proposal_id": proposal, "proposal_digest": row["digest"],
                   "source": {"id": source["id"], "digest": source["blob"], "content": source_text},
                   "requirement": {**requirement, "body": parse_json(requirement["body"])},
                   "requirement_acceptance_review": requirement_review,
                   "superseded_decisions": superseded,
                   "required_coverage": [f"policy:{proposal}", f"source:{source['id']}", f"requirement:{requirement['id']}"],
                   "instructions": "Judge whether the original human source and accepted requirement authorize this exact policy adoption and whether the old/new policy diff is consistent. Cite actual source and requirement evidence; do not approve from lexical matches or owner assertions."}
        context["required_coverage"] += [f"supersede:{value['id']}" for value in body.get("supersedes", [])]
        empty = {"format": "snapshot.v1", "repos": {}, "digest": digest({"repos": {}})}
        return row["project"], binding, empty, context, None

    def policy_get(self, actor, project):
        self.k.project(actor, project)
        row = self.g.policy(project)
        return {**row, "body": row["body"], "adoptions": self.s.all("SELECT id,revision,digest,status,source,consistency_receipt,created FROM decisions WHERE project=? AND json_extract(body,'$.type')='execution_control_policy' ORDER BY created DESC", (project,))}

    def policy_apply(self, actor, proposal, expected_digest, review_receipt):
        actor.require("owner", "agent", project=self.s.one("SELECT project FROM decisions WHERE id=?", (proposal,), True)["project"])
        with self.s.transaction():
            row = self.s.one("SELECT * FROM decisions WHERE id=?", (proposal,), True)
            need(row["digest"] == expected_digest, "stale_digest", "Policy proposal digest differs")
            if row["status"] == "applied":
                need(row["consistency_receipt"] == review_receipt, "idempotency_conflict", "Applied policy adoption needs the same review receipt")
                return {"id": proposal, "status": "applied", "review_receipt": review_receipt, "replayed": True}
            need(row["status"] == "instruction_recorded", "invalid_state", "Policy adoption is not awaiting consistency review")
            body = parse_json(row["body"])
            need(body.get("type") == "execution_control_policy", "invalid_policy", "Use policy_apply only for execution-control policy adoption")
            old = self.g.policy(row["project"])
            need(old["revision"] == body["old_revision"] and old["digest"] == body["old_digest"], "stale_policy", "Policy changed since adoption was recorded")
            for value in body.get("supersedes", []):
                current_superseded = self.s.one("SELECT digest,status FROM decisions WHERE id=? AND project=?",
                                               (value["id"], row["project"]), True)
                need(current_superseded["digest"] == value["digest"] and current_superseded["status"] == value["status"],
                     "stale_policy", "A superseded pending policy decision changed")
            source, requirement, _requirement_review, _old, superseded = self._policy_refs(actor, row["project"],
                {"source": body["source"], "requirement": body["requirement"], "expected_policy": {"revision": old["revision"], "digest": old["digest"]},
                 "supersedes": [{"id": v["id"], "digest": v["digest"]} for v in body.get("supersedes", [])], "reason": body["reason"]})
            binding = self.policy_binding(proposal)
            self.g.require_review(review_receipt, proposal, binding, {"consistency"}, latest=True)
            review = self.g.receipt(review_receipt)
            covered = set(review["result"].get("covered", []))
            required = {f"policy:{proposal}", f"source:{source['id']}", f"requirement:{requirement['id']}"} | {f"supersede:{v['id']}" for v in body.get("supersedes", [])}
            need(required <= covered, "review_coverage", "Consistency review did not cover all policy markers")
            new = body["body"]
            self.s.execute("UPDATE policies SET revision=?,body=?,digest=? WHERE project=?", (old["revision"] + 1, canonical(new).decode(), digest(new), row["project"]))
            superseded_event = []
            for value in body.get("supersedes", []):
                previous = self.s.one("SELECT id,project,body,digest,status,response,source FROM decisions WHERE id=? AND project=?",
                                      (value["id"], row["project"]), True)
                previous_inbox = self.s.one("SELECT id,status,body FROM inbox WHERE project=? AND ref=?",
                                            (row["project"], value["id"]))
                superseded_event.append({"decision": previous["id"], "digest": previous["digest"],
                                        "status": previous["status"], "response": previous["response"],
                                        "source": previous["source"], "body": parse_json(previous["body"]),
                                        "inbox": {"id": previous_inbox["id"], "status": previous_inbox["status"],
                                                  "body": parse_json(previous_inbox["body"])} if previous_inbox else None})
                self.s.execute("UPDATE decisions SET status='superseded' WHERE id=? AND project=?", (value["id"], row["project"]))
                self.s.execute("UPDATE inbox SET status='resolved' WHERE project=? AND ref=?", (row["project"], value["id"]))
            self.s.execute("UPDATE decisions SET status='applied',consistency_receipt=? WHERE id=?", (review_receipt, proposal))
            self.s.execute("UPDATE inbox SET status='resolved' WHERE project=? AND ref=?", (row["project"], proposal))
            legacy_budget_blocks = [dict(value) for value in self.s.all(
                "SELECT task,kind,ref,reason FROM blocks WHERE kind='budget' AND ref='attempts' "
                "AND task IN (SELECT id FROM tasks WHERE project=?) ORDER BY task", (row["project"],))]
            self.s.execute("DELETE FROM blocks WHERE kind='budget' AND ref='attempts' AND task IN (SELECT id FROM tasks WHERE project=?)", (row["project"],))
            self.sec.event(row["project"], "execution_policy_applied", actor.id,
                           {"decision": proposal, "old_revision": old["revision"], "new_revision": old["revision"] + 1,
                            "source": body["source"], "requirement": body["requirement"], "review": review_receipt,
                            "supersedes": superseded_event,
                            "retained_legacy_budget_blocks": legacy_budget_blocks})
            return {"id": proposal, "status": "applied", "policy_revision": old["revision"] + 1,
                    "policy_digest": digest(new), "review_receipt": review_receipt, "replayed": False}

    def _task_binding(self, task, readonly=False):
        return self.g.task_binding(task, ensure_policy=not readonly)

    def _attempt(self, task, epoch):
        return self.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?", (task, epoch))

    def _historical_epochs(self, task, current_epoch):
        """Return epochs with retained claim/run evidence before this claim.

        A schema migration deliberately does not synthesize rows for dev17
        attempts.  The event and run indexes therefore remain part of the
        lookup, while an epoch is still only selected when actual evidence
        identifies it.
        """
        epochs = {row["attempt_epoch"] for row in self.s.all(
            "SELECT attempt_epoch FROM execution_attempts WHERE task=? AND attempt_epoch<?",
            (task, current_epoch))}
        epochs.update(row["epoch"] for row in self.s.all(
            "SELECT epoch FROM runs WHERE task=? AND role='implementer' AND epoch IS NOT NULL AND epoch<?",
            (task, current_epoch)))
        task_row = self.s.one("SELECT project FROM tasks WHERE id=?", (task,), True)
        for row in self.s.all("SELECT body FROM events WHERE project=? AND kind='task_claimed'", (task_row["project"],)):
            body = parse_json(row["body"])
            if body.get("task") == task and type(body.get("epoch")) is int and body["epoch"] < current_epoch:
                epochs.add(body["epoch"])
        return epochs

    def _historical_target(self, actor, task, current_epoch):
        epochs = self._historical_epochs(task, current_epoch)
        if not epochs:
            return None
        epoch = max(epochs)
        target = self._attempt(task, epoch)
        if target is None:
            target = self._legacy_target(actor, task, epoch)
        return target

    def _unresolved_recovery_target(self, actor, task, row=None, readonly=False):
        """Return the latest failed/unknown claim which still needs recovery.

        Recovery is a separate admission decision.  A normal replan may change
        semantic material and clear a task-definition block, but it cannot
        erase an unresolved lease/run history.  Once a reviewed recovery is
        consumed, the newly created attempt records that authorization and the
        old target is no longer an admission blocker.
        """
        row = row or self._task(actor, task)
        target = self._historical_target(actor, task, row["epoch"])
        if target is None:
            return None
        unresolved_status = target.get("status") in {"failed", "unknown", "claim_only", "claimed", "reserved", "running"}
        if not unresolved_status and not self._attempt_has_rejected_review(actor, task, target, readonly=readonly):
            return None
        current = self._attempt(task, row["epoch"])
        if current is not None:
            body = self._attempt_body(current)
            consumed = body.get("recovery_authorization")
            if consumed:
                return None
        return target

    def _target_timeout(self, target):
        """Read the observed timeout from the canonical implementer run."""
        run_id = target.get("implementer_run") or target.get("run")
        need(run_id, "attempt_unobserved", "The target attempt has no observed implementer run")
        run = self.s.one("SELECT body FROM runs WHERE id=?", (run_id,), True)
        body = parse_json(run["body"])
        value = body.get("timeout")
        return _require_duration(value, "observed attempt timeout")

    def _attempt_has_rejected_review(self, actor, task, target, readonly=False):
        """Detect an observed quality/spec review which rejected this attempt.

        An implementer can exit successfully while independent completion
        reviews reject the resulting candidate.  That is still unresolved
        attempt evidence and requires a separately reviewed recovery after a
        normal replan.  Match reviews through the durable run epoch rather
        than the current task binding: candidate collection and replanning
        intentionally change the latter.  Keep only the newest receipt per
        review role so a later valid re-review can supersede an earlier reject.
        """
        row = self._task(actor, task)
        _policy_row, policy = self._policy(row["project"], readonly=readonly)
        roles = set(policy.get("review_roles", []))
        if row["body"].get("risk") == "critical":
            roles.update(policy.get("critical_review_roles", []))
        if not roles:
            return False
        epoch = target.get("attempt_epoch", target.get("epoch"))
        rows = self.s.all(
            "SELECT q.body FROM receipts q JOIN runs r ON r.id=q.run "
            "WHERE q.subject=? AND r.task=? AND r.epoch=? ORDER BY q.created,q.id",
            (task, task, epoch),
        )
        latest = {}
        for item in rows:
            try:
                receipt = parse_json(item["body"])
            except (TypeError, ValueError):
                continue
            role = receipt.get("role")
            if role in roles:
                latest[role] = receipt
        for receipt in latest.values():
            result = receipt.get("result") or {}
            if not (
                receipt.get("exit_code") == 0
                and not any(receipt.get(key) for key in ("timed_out", "cancelled", "output_overflow", "failure"))
                and receipt.get("readonly_verified") is True
                and receipt.get("judgment_valid") is True
                and result.get("verdict") == "pass"
                and not result.get("findings")
            ):
                return True
        return False

    def _legacy_target(self, actor, task, epoch, ordinal_hint=None, run_id=None):
        """Reconstruct an existing dev17 implementer attempt without inventing a run.

        Schema migration intentionally does not manufacture execution_attempts
        rows.  Existing run/receipt evidence remains directly addressable by
        epoch, and the ordinal is taken from durable task-claimed events when
        available.  If those old events were not retained, the response marks
        the ordinal as unknown rather than pretending to know its rank.
        """
        task_row = self._task(actor, task)
        # Existing rows were never backfilled by schema migration.  Resolve
        # them only from their retained claim/run evidence; visible event count
        # is not an ordinal because the beginning of a legacy history may be
        # absent.
        claims = []
        for event in self.s.all("SELECT body FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (task_row["project"],)):
            value = parse_json(event["body"])
            if value.get("task") == task and value.get("epoch") == epoch:
                claims.append(value)
        if len({canonical({key: claim.get(key) for key in ("attempt_ordinal", "ordinal", "task_revision", "binding")})
                for claim in claims}) > 1:
            raise Fault("ambiguous_attempt", "Conflicting retained claim evidence requires explicit reconciliation")
        runs = self.s.all("""SELECT r.*,q.id AS receipt_id,q.body AS receipt_body
                           FROM runs r JOIN receipts q ON q.run=r.id
                           WHERE r.task=? AND r.epoch=? AND r.role='implementer'
                           ORDER BY r.start""", (task, epoch))
        if run_id is not None:
            runs = [value for value in runs if value["id"] == run_id]
        need(len(runs) <= 1, "ambiguous_attempt", "Multiple retained implementer runs require target_implementer_run")
        if not runs and not claims:
            return None
        run = runs[0] if runs else None
        ordinal = None
        revision = None
        binding = None
        receipt = None
        if run:
            receipt = self.g.receipt(run["receipt_id"])
            need(receipt.get("run") == run["id"] and receipt.get("task") == task,
                 "invalid_evidence", "Retained legacy receipt does not match its run")
            binding = run["binding"]
            run_body = parse_json(run["body"])
            # These values were added to the run body by dev18 but are accepted
            # for old records only when actually present.
            body_ordinal = run_body.get("attempt_ordinal")
            receipt_ordinal = receipt.get("attempt_ordinal")
            need(body_ordinal is None or receipt_ordinal is None or body_ordinal == receipt_ordinal,
                 "invalid_evidence", "Legacy run and receipt ordinals differ")
            body_revision = run_body.get("task_revision")
            receipt_revision = receipt.get("task_revision")
            need(body_revision is None or receipt_revision is None or body_revision == receipt_revision,
                 "invalid_evidence", "Legacy run and receipt revisions differ")
            ordinal = body_ordinal if body_ordinal is not None else receipt_ordinal
            revision = body_revision if body_revision is not None else receipt_revision
            if ordinal is not None:
                need(type(ordinal) is int and ordinal > 0, "invalid_evidence", "Legacy attempt ordinal is invalid")
            if revision is not None:
                need(type(revision) is int and revision > 0, "invalid_evidence", "Legacy task revision is invalid")
        if claims:
            claim = claims[-1]
            if ordinal is None:
                ordinal = claim.get("attempt_ordinal", claim.get("ordinal"))
            if revision is None:
                revision = claim.get("task_revision")
            claim_binding = claim.get("binding")
            need(binding is None or claim_binding is None or binding == claim_binding,
                 "invalid_evidence", "Legacy claim and run bindings differ")
            binding = binding or claim_binding
        # Caller hints never fill an unknown historical ordinal.
        if ordinal is None:
            ordinal_known = False
        else:
            ordinal_known = True
        status = "claim_only"
        if receipt:
            status = "succeeded" if receipt.get("exit_code") == 0 and not receipt.get("failure") else "failed"
        attempt_body = {"format": "daikibo.execution-attempt-legacy.v2", "task": task,
                        "project": task_row["project"], "epoch": epoch,
                        "ordinal": ordinal, "ordinal_known": ordinal_known,
                        "revision": revision, "revision_known": revision is not None,
                        "binding": binding, "implementer_run": run["id"] if run else None,
                        "implementer_receipt": run["receipt_id"] if run else None,
                        "status": status, "legacy_history": True,
                        "claim_evidence": claims}
        return {"id": f"legacy:{task}:{epoch}", "task": task, "project": task_row["project"],
                "attempt_epoch": epoch, "attempt_ordinal": ordinal,
                "task_revision": revision, "task_binding": binding,
                "status": status, "implementer_run": run["id"] if run else None,
                "implementer_receipt": run["receipt_id"] if run else None, "body": attempt_body,
                "legacy": True, "receipt": receipt, "claim_only": run is None}

    def _attempt_body(self, row):
        body = parse_json(row["body"])
        need(digest(body) == row["digest"], "integrity_error", "Execution attempt content differs")
        return body

    def _proposal_row(self, actor, proposal):
        row = self.s.one("SELECT * FROM execution_control_proposals WHERE id=?", (proposal,), True)
        self.k.project(actor, row["project"])
        body = parse_json(row["body"])
        need(digest(body) == row["digest"], "integrity_error", "Execution-control proposal content differs")
        need(digest({"proposal": proposal, "body": body}) == row["binding"],
             "integrity_error", "Execution-control proposal binding differs")
        row["body"] = body
        row["result"] = parse_json(row["result"]) if row["result"] else None
        return row

    def _current_material(self, actor, task, target, readonly=False):
        row = self._task(actor, task)
        plan = self.s.one("SELECT body,digest FROM plans WHERE task=?", (task,))
        plan_value = None
        if plan:
            plan_body = parse_json(plan["body"])
            need(digest(plan_body) == plan["digest"], "integrity_error", "Frozen test plan changed")
            plan_value = {"body": plan_body, "digest": plan["digest"]}
        reads = self.s.all("SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact", (task,))
        actual_reads = self.s.all("""SELECT r.artifact,a.revision,a.digest,a.status
                                   FROM task_reads r JOIN artifacts a ON a.id=r.artifact
                                   WHERE r.task=? ORDER BY r.artifact""", (task,))
        deps = self.s.all("""SELECT d.dependency,t.revision,t.status,t.validity,t.body AS body_json,
                                  c.digest AS candidate_digest
                            FROM task_deps d JOIN tasks t ON t.id=d.dependency
                            LEFT JOIN candidates c ON c.id=t.candidate
                            WHERE d.task=? ORDER BY d.dependency""", (task,))
        for dep in deps:
            dep["body_digest"] = digest(parse_json(dep.pop("body_json")))
        selected_run = target.get("implementer_run", target.get("run"))
        attempt = self._attempt(task, target["attempt_epoch"])
        if attempt is None:
            attempt = self._legacy_target(actor, task, target["attempt_epoch"],
                                          target.get("attempt_ordinal"), selected_run)
        need(attempt is not None, "attempt_not_recorded", "Target attempt has no durable claim or retained run record")
        if selected_run is not None:
            need(attempt.get("implementer_run") == selected_run, "stale_attempt",
                 "Target implementer run differs")
        attempt_body = attempt["body"] if attempt.get("legacy") else self._attempt_body(attempt)
        need(attempt["project"] == row["project"], "invalid_attempt", "Target attempt belongs to a different project")
        current_policy_row, policy = self._policy(row["project"], readonly=readonly)
        profile = self.s.one("SELECT body,digest FROM profiles WHERE project=?", (row["project"],))
        baseline = None
        if profile:
            profile_body = parse_json(profile["body"])
            baseline = profile_body.get("baseline_snapshot", {}).get("digest") if isinstance(profile_body.get("baseline_snapshot"), dict) else profile_body.get("baseline_snapshot")
        if baseline is None:
            # Use the same input capture as Runtime.task_snapshot when a
            # project has no delivery profile.  This is the current permission
            # baseline; a legacy receipt's historical snapshot is retained in
            # target_attempt and must never substitute for it.
            baseline = self.c.rt.task_snapshot(actor, row, store_blobs=not readonly)["digest"]
        # Keep this list identical to Runtime._invariants: constraints-only
        # artifacts are mandatory runtime inputs even outside task_reads.
        invariants = []
        for invariant in self.s.all("SELECT id,revision,digest,body FROM artifacts WHERE project=? AND status='accepted' ORDER BY id", (row["project"],)):
            invariant_body = parse_json(invariant["body"])
            if invariant_body.get("constraints") or invariant_body.get("critical"):
                invariants.append({"id": invariant["id"], "revision": invariant["revision"],
                                   "digest": invariant["digest"],
                                   "statement": invariant_body["statement"],
                                   "constraints": invariant_body.get("constraints", {})})
        semantic = {
            "task": task, "project": row["project"], "revision": row["revision"],
            "body": row["body"], "body_digest": digest(row["body"]),
            "test_plan": plan_value, "requirements": reads, "actual_requirements": actual_reads, "dependencies": deps,
            "baseline_snapshot": baseline, "accepted_invariants": invariants,
            "policy": {"revision": current_policy_row["revision"], "digest": current_policy_row["digest"],
                        "body": policy},
        }
        return {
            "format": "daikibo.execution-control-material.v1",
            "task": {
                "id": task, "project": row["project"], "revision": row["revision"],
                "epoch": row["epoch"], "body": row["body"], "body_digest": digest(row["body"]),
                "binding": self._task_binding(task, readonly=readonly), "attempts": row["attempts"],
                "no_progress_count": row["no_progress_count"],
            },
            "target_attempt": {
                "id": attempt["id"], "epoch": attempt["attempt_epoch"],
                "ordinal": attempt["attempt_ordinal"], "revision": attempt["task_revision"],
                "binding": attempt["task_binding"], "status": attempt["status"],
                "implementer_run": attempt["implementer_run"],
                "implementer_receipt": attempt["implementer_receipt"],
                "claim_only": bool(attempt.get("claim_only")),
                "legacy": bool(attempt.get("legacy")), "body": attempt_body,
            },
            "test_plan": plan_value,
            "requirements": reads,
            "dependencies": deps,
            "policy": {"revision": current_policy_row["revision"], "digest": current_policy_row["digest"],
                        "body": policy},
            "semantic": semantic,
            "semantic_digest": digest(semantic),
            "assessment": self.s.one("SELECT id,judgment,reviewer_run,reviewer_receipt,digest FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                                      (task, target["attempt_epoch"])),
        }

    def _evidence(self, actor, project, evidence):
        need(isinstance(evidence, list) and evidence, "invalid_evidence", "Proposal needs evidence references")
        result = []
        for ref in evidence:
            if isinstance(ref, str):
                text(ref, "evidence reference", 512)
                ident = ref
                value = {"id": ref}
            else:
                obj(ref, required=("id",), optional=("digest", "revision", "kind"), name="evidence reference")
                text(ref["id"], "evidence reference", 512)
                ident = ref["id"]; value = dict(ref)
            row = self.s.one("SELECT project,digest,revision,status FROM artifacts WHERE id=?", (ident,))
            kind = "artifact"
            if row is None:
                row = self.s.one("SELECT project,blob AS digest FROM sources WHERE id=?", (ident,))
                kind = "source"
            if row is None:
                receipt = self.s.one("SELECT project,subject,binding FROM receipts WHERE id=?", (ident,))
                row = receipt
                kind = "receipt"
            if row is None:
                event = self.s.one("SELECT project,body FROM events WHERE id=?", (ident,))
                if event:
                    event = {**event, "digest": digest(parse_json(event["body"]))}
                    value.setdefault("digest", event["digest"])
                    row = event
                    kind = "event"
            if row is None:
                job = self.s.one("SELECT project,args FROM jobs WHERE id=?", (ident,))
                if job:
                    job = {**job, "digest": digest(parse_json(job["args"]))}
                    value.setdefault("digest", job["digest"])
                    row = job
                    kind = "job"
            need(row is not None and row["project"] == project, "invalid_evidence", "Evidence is absent or belongs elsewhere", ident)
            if value.get("digest") is not None:
                need(value["digest"] == row.get("digest") or value["digest"] == row.get("binding"),
                     "stale_evidence", "Evidence digest differs", ident)
            if value.get("kind") is not None:
                need(value["kind"] == kind, "invalid_evidence", "Evidence kind differs", ident)
            result.append({**value, "id": ident, "kind": kind})
        return result

    def _assert_target(self, actor, task, body):
        row = self._task(actor, task)
        need(type(body.get("target_attempt_epoch")) is int and body["target_attempt_epoch"] >= 0,
             "invalid_attempt", "target_attempt_epoch must be a nonnegative integer")
        control_type = body["control_type"]
        target = self._attempt(task, body["target_attempt_epoch"])
        if target is None:
            target = self._legacy_target(actor, task, body["target_attempt_epoch"],
                                         body.get("target_attempt_ordinal"), body.get("target_implementer_run"))
        need(target is not None, "attempt_not_recorded", "Target attempt has no durable claim or retained run record")
        ordinal = body.get("target_attempt_ordinal")
        if ordinal is not None:
            need(type(ordinal) is int and ordinal > 0, "invalid_attempt", "target_attempt_ordinal must be positive when supplied")
            need(target.get("attempt_ordinal") == ordinal, "stale_attempt", "Attempt ordinal differs")
        if body.get("target_implementer_run") is not None:
            need(target.get("implementer_run") == body["target_implementer_run"], "stale_attempt", "Target implementer run differs")
        if control_type in {"assessment", "timeout"}:
            need(target.get("implementer_run") and target.get("implementer_receipt"),
                 "attempt_unobserved", "This execution-control proposal requires an observed implementer receipt")
        if control_type == "recovery" and target.get("claim_only"):
            # A claim-only recovery is intentionally not an attempt judgment.
            # It can only be a reviewed admission/recovery decision.
            need(target.get("implementer_run") is None and target.get("implementer_receipt") is None,
                 "invalid_attempt", "Claim-only recovery target is inconsistent")
        if control_type == "recovery" and not target.get("implementer_run"):
            need(row["epoch"] != target["attempt_epoch"] or row["status"] != "running"
                 or row["lease_until"] is None or row["lease_until"] <= timestamp(),
                 "recovery_not_needed", "A live unexpired claim cannot be recovered")
        if body.get("old_effective_seconds") is not None:
            old = _require_duration(body["old_effective_seconds"], "old_effective_seconds")
            observed = self._target_timeout(target)
            need(observed == old, "stale_timeout",
                 "old_effective_seconds differs from the observed target timeout")
        return row, target

    # ---------- claims and collector binding ----------

    def recovery_authorization(self, actor, task, readonly=False):
        """Return the latest current recovery approval for the last failed lease/run."""
        row = self._task(actor, task)
        # A recovery grant is usable only for the immediately preceding
        # durable claim/run.  Older approvals remain history and must not
        # bypass a newer failed or claim-only attempt.
        historical_epochs = self._historical_epochs(task, row["epoch"])
        latest_epoch = max(historical_epochs) if historical_epochs else None
        auths = self.s.all("SELECT * FROM execution_control_authorizations WHERE task=? ORDER BY control_revision DESC,created DESC", (task,))
        for auth in auths:
            body = parse_json(auth["body"])
            if body.get("control_type") != "recovery":
                continue
            target = body.get("target_attempt", {})
            epoch = target.get("epoch")
            if type(epoch) is not int or epoch >= row["epoch"]:
                continue
            if latest_epoch is None or epoch != latest_epoch:
                continue
            if not target.get("run") or not target.get("receipt"):
                if auth["requested_seconds"] is not None or auth["effective_seconds"] is not None or auth["assessment"] is not None:
                    continue
            if body.get("proposal_digest") != auth["proposal_digest"] or digest(body) != auth["digest"]:
                continue
            # A recovery grant is one-use admission evidence.  Once a later
            # claim records its id, this authorization remains history but
            # cannot be reused for another claim or retry.
            consumed = False
            for claim in self.s.all("SELECT body,digest FROM execution_attempts WHERE task=?", (task,)):
                if self._attempt_body(claim).get("recovery_authorization") == auth["id"]:
                    consumed = True
                    break
            if consumed:
                continue
            try:
                material = self._current_material(actor, task, {"attempt_epoch": epoch,
                                                                 "attempt_ordinal": target.get("ordinal"),
                                                                 "task_revision": target.get("revision"),
                                                                 "implementer_run": target.get("run")},
                                                   readonly=readonly)
            except Fault:
                continue
            if body.get("semantic_digest") and body["semantic_digest"] != material.get("semantic_digest"):
                continue
            return {"id": auth["id"], "digest": auth["digest"], "target_epoch": epoch,
                    "target_ordinal": target.get("ordinal"), "assessment": auth["assessment"]}
        return None

    def admission(self, actor, task, readonly=False):
        row = self._task(actor, task)
        threshold = self._threshold(row, readonly=readonly)
        policy_row, policy = self._policy(row["project"], readonly=readonly)
        failures = []
        if row["no_progress_count"] >= threshold:
            failures.append("no_progress_limit")
        # A deprecated budget/attempts block is telemetry history after policy
        # v2 adoption; all other currentness and recovery gates remain active.
        current = self.g.check_current(task, ensure_policy=not readonly)
        recovery = self.recovery_authorization(actor, task, readonly=readonly)
        unresolved = self._unresolved_recovery_target(actor, task, row, readonly=readonly)
        if unresolved is not None and recovery is None:
            failures.append("recovery_required:" + str(unresolved.get("attempt_epoch")))
        for failure in current:
            # Legacy attempt-budget and a reviewed recovery-required lease gate
            # remain durable history; only the matching recovery authorization
            # allows a fresh normal admission check to proceed.
            if failure == "block:budget:attempts" and policy.get("version", 1) >= 2:
                continue
            if failure.startswith("block:run_unknown:") and recovery is not None:
                continue
            failures.append(failure)
        return {"allowed": not failures, "failures": failures,
                "threshold": threshold, "no_progress_count": row["no_progress_count"],
                "attempts": row["attempts"], "legacy_attempt_limit_blocking": False}

    def record_claim(self, actor, task, epoch, ordinal=None):
        """Insert the durable attempt row inside the claim transaction."""
        row = self._task(actor, task)
        need(row["status"] == "running" and row["epoch"] == epoch, "stale_lease", "Claim is no longer current")
        ordinal = row["attempts"] if ordinal is None else ordinal
        binding = self._task_binding(task)
        recovery = self.recovery_authorization(actor, task)
        ident = uid("XATT")
        body = {"format": "daikibo.execution-attempt.v1", "task": task, "project": row["project"],
                "epoch": epoch, "ordinal": ordinal, "revision": row["revision"], "binding": binding,
                "legacy_history": False, "claim_actor": actor.id,
                "recovery_authorization": recovery["id"] if recovery else None}
        self.s.execute("""INSERT INTO execution_attempts
            (id,task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,status,
             implementer_run,implementer_receipt,body,digest,created,updated)
            VALUES(?,?,?,?,?,?,?, 'claimed',NULL,NULL,?,?,?,?)""",
                       (ident, task, row["project"], epoch, ordinal, row["revision"], binding,
                        canonical(body).decode(), digest(body), timestamp(), timestamp()))
        if recovery is not None:
            # The reviewed recovery is consumed by this exact claim.  Remove
            # only the matching operational blocker; the authorization and
            # claim body remain durable history for later export/audit.
            self.s.execute("DELETE FROM blocks WHERE task=? AND kind='run_unknown'", (task,))
            self.sec.event(row["project"], "execution_control_recovery_consumed", actor.id,
                           {"task": task, "attempt": ident, "authorization": recovery["id"],
                            "target_epoch": recovery["target_epoch"]})
        return {"id": ident, "task": task, "epoch": epoch, "ordinal": ordinal, "binding": binding}

    def reserve_implementer(self, actor, task, epoch):
        row = self._task(actor, task)
        actor.require("owner", "agent", "worker", project=row["project"], task=task if actor.task else None)
        need(row["status"] == "running" and row["epoch"] == epoch, "stale_lease", "Claim is no longer current")
        ident = uid("RUN")
        with self.s.transaction():
            attempt = self._attempt(task, epoch)
            if attempt is None:
                attempt = self.record_claim(actor, task, epoch, row["attempts"])
                attempt = self._attempt(task, epoch)
            need(attempt["implementer_run"] is None and attempt["status"] == "claimed", "attempt_already_executing",
                 "This attempt already has an implementer run", attempt["implementer_run"])
            updated = self.s.execute("UPDATE execution_attempts SET status='reserved',updated=? WHERE task=? AND attempt_epoch=? AND status='claimed' AND implementer_run IS NULL",
                                     (timestamp(), task, epoch)).rowcount
            need(updated == 1, "attempt_already_executing", "A concurrent implementer already owns this attempt")
        return ident

    def bind_implementer(self, task, epoch, run_id):
        """Bind a reserved attempt only after its durable run row exists."""
        with self.s.transaction():
            changed = self.s.execute("""UPDATE execution_attempts SET implementer_run=?,status='running',updated=?
                                        WHERE task=? AND attempt_epoch=? AND status='reserved'
                                          AND implementer_run IS NULL""",
                                     (run_id, timestamp(), task, epoch)).rowcount
            need(changed == 1, "attempt_already_executing", "Implementer reservation was lost or already bound")

    def finalize_attempt(self, task, epoch, receipt):
        row = self.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?", (task, epoch))
        if row is None:
            return None
        status = "succeeded" if receipt and receipt.get("exit_code") == 0 and not receipt.get("failure") \
            and not any(receipt.get(k) for k in ("timed_out", "cancelled", "output_overflow")) else "failed"
        if not receipt:
            status = "unknown"
        run = receipt.get("run") if receipt else row["implementer_run"]
        evidence = receipt.get("id") if receipt else None
        with self.s.transaction():
            self.s.execute("UPDATE execution_attempts SET implementer_run=coalesce(implementer_run,?),implementer_receipt=coalesce(implementer_receipt,?),status=?,updated=? WHERE task=? AND attempt_epoch=?",
                           (run, evidence, status, timestamp(), task, epoch))
        return self.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?", (task, epoch))

    # ---------- proposal / packets ----------

    def _normalize_proposal(self, body):
        # The frozen contract has one duration field.  Do not silently accept
        # aliases: a caller must know whether it is requesting a timeout.
        obj(body, required=("target_attempt_epoch", "control_type", "cause_analysis", "experiment_estimate",
                            "evidence", "intended_next_action", "scope"),
            optional=("target_attempt_ordinal", "target_implementer_run", "old_effective_seconds",
                      "requested_seconds", "recovery_action"))
        need(type(body["target_attempt_epoch"]) is int and body["target_attempt_epoch"] >= 0,
             "invalid_attempt", "target_attempt_epoch must be a nonnegative integer")
        body = dict(body)
        need(body["control_type"] in {"assessment", "recovery", "timeout"}, "invalid_control_type", "Unknown execution-control proposal type")
        if body.get("target_attempt_ordinal") is not None:
            need(type(body["target_attempt_ordinal"]) is int and body["target_attempt_ordinal"] > 0,
                 "invalid_attempt", "target_attempt_ordinal must be positive when supplied")
        if body.get("target_implementer_run") is not None:
            text(body["target_implementer_run"], "target_implementer_run", 200)
        body["old_effective_seconds"] = body.get("old_effective_seconds")
        if body["old_effective_seconds"] is not None:
            body["old_effective_seconds"] = _require_duration(body["old_effective_seconds"], "old_effective_seconds")
        body["requested_seconds"] = body.get("requested_seconds")
        if body["control_type"] == "timeout":
            need(body["requested_seconds"] is not None, "invalid_timeout", "Timeout proposal needs requested_seconds")
            body["requested_seconds"] = _require_duration(body["requested_seconds"], "requested_seconds")
        else:
            need(body["requested_seconds"] is None, "invalid_timeout", "Assessment and recovery proposals cannot request a duration")
        if body["control_type"] == "recovery":
            need(isinstance(body.get("recovery_action"), str) and bool(body["recovery_action"].strip()),
                 "invalid_input", "Recovery proposal needs recovery_action")
        text(body["cause_analysis"], "cause_analysis", 40_000)
        text(body["intended_next_action"], "intended_next_action", 20_000)
        if body.get("recovery_action") is not None:
            text(body["recovery_action"], "recovery_action", 20_000)
        need(isinstance(body["experiment_estimate"], (dict, str)), "invalid_input", "experiment_estimate must be an object or string")
        if isinstance(body["experiment_estimate"], str): text(body["experiment_estimate"], "experiment_estimate", 20_000)
        need(isinstance(body["scope"], (dict, str)), "invalid_input", "scope must be an object or string")
        if isinstance(body["scope"], str): text(body["scope"], "scope", 20_000)
        return body

    def propose(self, actor, task, expected_revision, body):
        need(type(expected_revision) is int and expected_revision > 0, "invalid_revision", "Expected task revision must be positive")
        body = self._normalize_proposal(body)
        with self.s.transaction():
            scoped_row = self._task(actor, task)
            actor.require("owner", "agent", project=scoped_row["project"], task=task)
            row, target = self._assert_target(actor, task, body)
            need(row["revision"] == expected_revision, "stale_revision", "Task revision changed")
            material = self._current_material(actor, task, target)
            material_digest = digest(material)
            evidence = self._evidence(actor, row["project"], body["evidence"])
            # The immutable body captures user supplied causes plus exact current
            # bindings; all later authorization checks re-read these values.
            payload = {"format": "daikibo.execution-control-proposal.v1", "task": task,
                       "project": row["project"], "task_revision": row["revision"],
                       "request": {**body, "evidence": evidence}, "material": material,
                       "material_digest": material_digest,
                       "target_attempt": {"epoch": target["attempt_epoch"], "ordinal": target["attempt_ordinal"],
                                           "revision": target.get("task_revision"),
                                           "binding": target.get("task_binding"),
                                           "run": target["implementer_run"], "receipt": target["implementer_receipt"],
                                           "claim_only": bool(target.get("claim_only")),
                                           "legacy": bool(target.get("legacy"))},
                       "control_type": body["control_type"],
                       "policy_default_seconds": self._policy(row["project"])[1]["default_task_timeout_seconds"]}
            need(len(canonical(payload)) <= 900_000, "context_insufficient", "Execution-control material needs bounded packet review")
            ident = uid("XCP")
            binding = digest({"proposal": ident, "body": payload})
            self.s.execute("INSERT INTO execution_control_proposals VALUES(?,?,?,?,?,?,?,'proposed',NULL,?)",
                           (ident, task, row["project"], row["revision"], canonical(payload).decode(), digest(payload), binding, timestamp()))
            # One packet is sufficient for normal material; packet() still
            # provides an exact paginated contract and refuses stale snapshots.
            coverage = [f"attempt:{target['attempt_epoch']}" ]
            if body["control_type"] == "timeout":
                coverage.append(f"timeout:{ident}")
            elif body["control_type"] == "recovery":
                coverage = [f"recovery:{ident}"] + ([f"attempt:{target['attempt_epoch']}"] if target.get("implementer_run") else [])
            packet_body = {"format": "daikibo.execution-control-packet.v1", "proposal": ident,
                           "proposal_digest": digest(payload), "ordinal": 0, "start": 0, "end": 1,
                           "total": 1, "material_digest": material_digest, "required_coverage": coverage,
                           "material": material}
            packet_id = uid("XCP-PACKET")
            self.s.execute("INSERT INTO execution_control_packets VALUES(?,?,?,?,?,?,?)",
                           (packet_id, ident, row["project"], 0, canonical(packet_body).decode(), digest(packet_body), timestamp()))
            self.sec.event(row["project"], "execution_control_proposed", actor.id,
                           {"proposal": ident, "task": task, "digest": digest(payload),
                            "target_attempt": target["attempt_epoch"], "requested_seconds": body["requested_seconds"]})
        return self.get(actor, ident)

    def get(self, actor, proposal):
        row = self._proposal_row(actor, proposal)
        row["packets"] = [{"id": r["id"], "ordinal": r["ordinal"], "digest": r["digest"]}
                          for r in self.s.all("SELECT id,ordinal,digest FROM execution_control_packets WHERE proposal=? ORDER BY ordinal", (proposal,))]
        row["events"] = self.s.all("SELECT id,kind,digest,created FROM execution_control_events WHERE proposal=? ORDER BY created,id", (proposal,))
        row["authorizations"] = self.s.all("SELECT id,task_revision,control_revision,requested_seconds,effective_seconds,assessment,reviewer_receipt,digest,created FROM execution_control_authorizations WHERE proposal=? ORDER BY created,id", (proposal,))
        row["target_attempt_epoch"] = row["body"]["target_attempt"]["epoch"]
        row["target_attempt_ordinal"] = row["body"]["target_attempt"].get("ordinal")
        row["requested_seconds"] = row["body"]["request"].get("requested_seconds")
        row["material_digest"] = row["body"].get("material_digest")
        row["packet_manifest"] = list(row["packets"])
        return row

    def inventory(self, actor, task=None, project=None, offset=0, limit=100, expected_snapshot=None):
        """Return a bounded, stable view of pending controls and attempt telemetry."""
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             "invalid_range", "Use a bounded execution-control inventory page")
        if task is not None:
            row = self._task(actor, task)
            project = row["project"]
            proposals = self.s.all("SELECT id,task,project,task_revision,digest,status,created FROM execution_control_proposals WHERE task=? ORDER BY created,id", (task,))
            history = self.history(actor, task, 0, 200)
            attempts = history["attempts"]
            items = ([{"kind": "attempt", **value} for value in attempts] +
                     [{"kind": "assessment", **value} for value in history["assessments"]] +
                     [{"kind": "proposal", **value} for value in history["proposals"]])
            admission = self.admission(actor, task)
            current = {"revision": row["revision"], "epoch": row["epoch"], "binding": self._task_binding(task),
                       "attempts": row["attempts"], "no_progress_count": row["no_progress_count"],
                       "policy": {"default_task_timeout_seconds": self._policy(project)[1]["default_task_timeout_seconds"],
                                  "max_no_progress_attempts": admission["threshold"]}}
        else:
            need(project is not None, "invalid_input", "Inventory needs task or project")
            self.k.project(actor, project)
            proposals = self.s.all("SELECT id,task,project,task_revision,digest,status,created FROM execution_control_proposals WHERE project=? ORDER BY created,id", (project,))
            attempts = self.s.all("SELECT id,task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,status,implementer_run,implementer_receipt,digest,created,updated FROM execution_attempts WHERE project=? ORDER BY created,id", (project,))
            items = ([{"kind": "attempt", **value} for value in attempts] +
                     [{"kind": "proposal", **value} for value in proposals])
            current = None
            admission = None
        value = {"proposals": proposals, "attempts": attempts, "items": items}
        stamp = digest(value)
        need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
             "stale_inventory", "Execution-control inventory changed between pages")
        page = items[offset:offset + limit]
        return {"task": task, "project": project, "current": current, "items": page,
                "proposals": proposals, "attempts": attempts,
                "telemetry": {"attempts": current["attempts"] if current else len(attempts),
                              "no_progress_count": current["no_progress_count"] if current else None,
                              "legacy_attempt_limit_blocking": False},
                "admission": admission, "snapshot": stamp, "total": len(items),
                "next_offset": offset + limit if offset + limit < len(items) else None}

    def packet(self, actor, proposal, offset=0, limit=1, expected_snapshot=None):
        row = self._proposal_row(actor, proposal)
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
             "invalid_range", "Use a bounded packet page")
        rows = self.s.all("SELECT id,proposal,project,ordinal,body,digest,created FROM execution_control_packets WHERE proposal=? ORDER BY ordinal", (proposal,))
        stamp = digest([{"id": r["id"], "ordinal": r["ordinal"], "digest": r["digest"]} for r in rows])
        need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
             "stale_packet_list", "Execution-control packet list changed")
        out = []
        for item in rows[offset:offset + limit]:
            body = parse_json(item["body"])
            need(digest(body) == item["digest"] and body.get("proposal") == proposal,
                 "integrity_error", "Execution-control packet content differs")
            out.append({**item, "body": body})
        return {"proposal": proposal, "proposal_digest": row["digest"], "packets": out,
                "snapshot": stamp, "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None}

    def _proposal_current(self, actor, row):
        body = row["body"]
        task = self._task(actor, row["task"])
        # A historical assessment does not grant current permission and may
        # finish after a task replan.  Recovery/timeout authorizations are
        # current permissions and must still match the proposal revision.
        if body.get("control_type") != "assessment":
            need(task["revision"] == row["task_revision"], "stale_revision", "Task revision changed since proposal")
        target = {"attempt_epoch": body["target_attempt"]["epoch"],
                  "attempt_ordinal": body["target_attempt"].get("ordinal"),
                  "task_revision": body["target_attempt"].get("revision"),
                  "implementer_run": body["target_attempt"].get("run",
                                                                    body["target_attempt"].get("implementer_run"))}
        material = self._current_material(actor, row["task"], target)
        # An assessment records the independent judgment of historical
        # evidence and may finish after a replan.  Timeout/recovery controls
        # grant current permission and therefore revalidate semantic inputs.
        if body.get("control_type") != "assessment":
            need(digest(material.get("semantic")) == body["material"].get("semantic_digest"),
                 "stale_execution_control", "Task inputs or policy changed since proposal")
        return task, material

    def review_subject(self, actor, proposal, role):
        need(role == "execution_control", "invalid_role", "Execution-control material needs execution_control review")
        row = self._proposal_row(actor, proposal)
        need(row["status"] == "proposed", "invalid_state", "Execution-control proposal is no longer pending")
        page = self.packet(actor, proposal, 0, 100)
        control_type = row["body"].get("control_type", "timeout")
        if control_type == "recovery":
            required = [f"recovery:{proposal}"]
            observed_run = row["body"]["target_attempt"].get("run")
            if observed_run:
                required.append(f"attempt:{row['body']['target_attempt']['epoch']}")
                instructions = ("Assess whether the retained claim/run/lease evidence justifies recovery admission. "
                                "Return one typed recovery disposition for the recovery marker and one typed attempt "
                                "disposition for the retained implementer evidence. Assess the attempt independently "
                                "of the recovery admission decision; progress/no_progress requires semantic evidence, "
                                "and an inconclusive attempt remains nonfinal. Recovery approval does not authorize "
                                "a longer timeout, enqueue work, or bypass current gates.")
            else:
                instructions = ("Assess whether the retained claim/lease evidence justifies recovery admission. "
                                "Claim-only evidence must not be classified as progress or no-progress. Return only "
                                "the typed recovery disposition; approval permits a later normal reassessment and "
                                "does not enqueue or bypass current gates.")
        elif control_type == "assessment":
            required = [f"attempt:{row['body']['target_attempt']['epoch']}"]
            instructions = "Assess the actual retained implementer receipt and classify the exact attempt as progress, no_progress, or inconclusive. A changed byte, token total, test result, or quality failure alone is not semantic progress/no-progress evidence. An inconclusive result remains nonfinal and consumes no assessment slot."
        else:
            required = [f"attempt:{row['body']['target_attempt']['epoch']}", f"timeout:{proposal}"]
            instructions = "Assess the actual retained implementer receipt and cause/evidence, then independently decide whether the requested finite timeout is approved. A changed byte, token total, test result, or quality failure alone is not semantic progress/no-progress evidence. An inconclusive attempt result remains nonfinal; timeout approval is a separate typed decision."
        context = {"proposal": row["body"], "proposal_id": proposal, "proposal_digest": row["digest"],
                   "packets": page["packets"], "packet_snapshot": page["snapshot"],
                   "required_coverage": required, "instructions": instructions + " Overall pass certifies review validity, not implementation success."}
        existing = self.s.one("SELECT id,judgment,digest FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                              (row["task"], row["body"]["target_attempt"]["epoch"]))
        if existing:
            context["finalized_assessment"] = existing
            context["instructions"] += " The target already has a finalized assessment; preserve its exact judgment and cite its id/digest when reviewing a later control."
        empty = {"format": "snapshot.v1", "repos": {}, "digest": digest({"repos": {}})}
        return row["project"], row["binding"], empty, context, None

    def _dispositions(self, review, proposal):
        result = review.get("result", {})
        need(result.get("verdict") == "pass", "review_failed", "Execution-control review must pass to finalize an authorization")
        need(not result.get("findings"), "review_failed", "Execution-control review has unresolved findings")
        control_type = proposal["body"].get("control_type", "timeout")
        if control_type == "recovery":
            required = {f"recovery:{proposal['id']}"}
            if proposal["body"]["target_attempt"].get("run"):
                required.add(f"attempt:{proposal['body']['target_attempt']['epoch']}")
        elif control_type == "assessment":
            required = {f"attempt:{proposal['body']['target_attempt']['epoch']}"}
        else:
            required = {f"attempt:{proposal['body']['target_attempt']['epoch']}", f"timeout:{proposal['id']}"}
        need(required <= set(result.get("covered", [])), "review_coverage", "Review must cover the exact attempt and timeout markers")
        found = {}
        for item in result.get("dispositions", []):
            need(item.get("id") in required, "review_coverage", "Review contains an unexpected execution-control marker")
            need(item.get("id") not in found, "invalid_review", "Duplicate execution-control disposition")
            text(item.get("reason"), "disposition rationale", 40_000)
            found[item["id"]] = item.get("resolution")
        need(set(found) == required, "review_coverage", "Typed attempt and timeout dispositions are required")
        attempt_judgment = found.get(f"attempt:{proposal['body']['target_attempt']['epoch']}", "inconclusive")
        timeout_judgment = found.get(f"timeout:{proposal['id']}", "inconclusive")
        recovery_judgment = found.get(f"recovery:{proposal['id']}", "inconclusive")
        need(attempt_judgment in {"progress", "no_progress", "inconclusive"}, "invalid_review", "Unknown attempt disposition")
        need(timeout_judgment in {"approved", "rejected", "inconclusive"}, "invalid_review", "Unknown timeout disposition")
        need(recovery_judgment in {"approved", "rejected", "inconclusive"}, "invalid_review", "Unknown recovery disposition")
        existing = self.s.one("SELECT id,judgment,digest FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                              (proposal["task"], proposal["body"]["target_attempt"]["epoch"]))
        if existing:
            need(attempt_judgment == existing["judgment"], "assessment_conflict",
                 "A finalized attempt assessment must be referenced with its exact judgment")
        return attempt_judgment, timeout_judgment, recovery_judgment

    def _assessment(self, actor, proposal, review, target, judgment):
        if judgment == "inconclusive":
            return None
        epoch = target.get("attempt_epoch", target.get("epoch"))
        ordinal = target.get("attempt_ordinal", target.get("ordinal"))
        revision = target.get("task_revision", target.get("revision"))
        binding = target.get("task_binding", target.get("binding"))
        existing = self.s.one("SELECT * FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                              (proposal["task"], epoch))
        if existing:
            need(existing["judgment"] == judgment, "assessment_conflict",
                 "A different conclusive judgment already finalized this attempt")
            return existing
        observed = self.g.receipt(target["implementer_receipt"])
        need(observed["run"] == target["implementer_run"] and observed["subject"] == proposal["task"]
             and observed.get("role") == "implementer" and observed.get("task") == proposal["task"],
             "invalid_evidence", "Implementer receipt does not match the attempt")
        rationale = review["result"].get("rationale", "")
        evidence = review["result"].get("observations", [])
        ident = uid("XASSESS")
        body = {"format": "daikibo.attempt-assessment.v1", "task": proposal["task"],
                "project": proposal["project"], "attempt_epoch": epoch,
                "attempt_ordinal": ordinal, "task_revision": revision,
                "task_binding": binding, "proposal": proposal["id"],
                "proposal_digest": proposal["digest"], "implementer_run": target["implementer_run"],
                "implementer_receipt": target["implementer_receipt"], "reviewer_run": review["run"],
                "reviewer_receipt": review["receipt"], "judgment": judgment,
                "rationale": rationale, "evidence": evidence}
        self.s.execute("""INSERT INTO attempt_assessments
            (id,task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,proposal,proposal_digest,
             implementer_run,implementer_receipt,reviewer_run,reviewer_receipt,judgment,rationale,evidence,body,digest,created)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (ident, proposal["task"], proposal["project"], epoch, ordinal,
                        revision, binding, proposal["id"], proposal["digest"],
                        target["implementer_run"], target["implementer_receipt"], review["run"], review["receipt"],
                        judgment, rationale, canonical(evidence).decode(), canonical(body).decode(), digest(body), timestamp()))
        if judgment == "no_progress":
            updated = self.s.execute("UPDATE tasks SET no_progress_count=no_progress_count+1,updated=? WHERE id=?",
                                     (timestamp(), proposal["task"])).rowcount
            need(updated == 1, "not_found", "Task disappeared while recording no-progress assessment")
            row = self.s.one("SELECT no_progress_count FROM tasks WHERE id=?", (proposal["task"],), True)
            if row["no_progress_count"] >= self._threshold(self._task(actor, proposal["task"])):
                self.s.execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?,?)",
                               (proposal["task"], "no_progress", proposal["id"],
                                "Three independently reviewed no-progress attempts block further implementation admission"))
        return self.s.one("SELECT * FROM attempt_assessments WHERE id=?", (ident,))

    def apply(self, actor, proposal, expected_digest, review_receipt):
        with self.s.transaction():
            row = self._proposal_row(actor, proposal)
            actor.require("owner", "agent", project=row["project"], task=row["task"])
            need(row["digest"] == expected_digest, "stale_digest", "Execution-control proposal digest differs")
            if row["status"] == "applied":
                need(row["result"] and row["result"].get("review_receipt") == review_receipt,
                     "idempotency_conflict", "Applied proposal is replayable only with its recorded review")
                return {**row["result"], "replayed": True}
            need(row["status"] == "proposed", "invalid_state", "Execution-control proposal is not pending")
            task, material = self._proposal_current(actor, row)
            refs = self.g.evidence_for(proposal, row["binding"], "execution_control")
            need(refs and refs[0]["id"] == review_receipt, "review_required", "Use the latest execution-control review")
            # receipt() verifies storage integrity only.  require_review()
            # enforces the observed successful, readonly, schema-valid and
            # (in governed mode) qualified independent review before any
            # assessment counter or authorization can be written.
            review = self.g.require_review(review_receipt, proposal, row["binding"], {"execution_control"})
            need(review["run"] != material["target_attempt"].get("implementer_run"), "review_not_independent", "Reviewer must be a distinct observed run")
            attempt_judgment, timeout_judgment, recovery_judgment = self._dispositions(review, row)
            control_type = row["body"].get("control_type", "timeout")
            target = material["target_attempt"]
            old_effective = row["body"]["request"].get("old_effective_seconds")
            if old_effective is not None:
                old_effective = _require_duration(old_effective, "old_effective_seconds")
                need(self._target_timeout(target) == old_effective, "stale_timeout",
                     "old_effective_seconds differs from the observed target timeout")
            # A review with no decisive disposition is retained as an ordinary
            # receipt.  Keeping the proposal pending allows later evidence to
            # produce the one conclusive assessment for this epoch.
            # Each control marker has its own finality.  A conclusive attempt
            # classification is retained even when recovery/timeout permission
            # is still inconclusive; it must never silently disappear, but it
            # also cannot authorize the independent control by itself.
            if target.get("implementer_run") and target.get("implementer_receipt") and attempt_judgment != "inconclusive":
                assessment = self._assessment(actor, row, {"receipt": review_receipt, "run": review["run"], "result": review["result"]},
                                               target, attempt_judgment)
            else:
                assessment = None
            decisive = (attempt_judgment != "inconclusive" if control_type == "assessment"
                        else recovery_judgment != "inconclusive" if control_type == "recovery"
                        else timeout_judgment != "inconclusive")
            if not decisive:
                return {"proposal": proposal, "proposal_digest": row["digest"],
                        "status": "proposed", "review_receipt": review_receipt,
                        "assessment": assessment["id"] if assessment else None,
                        "authorization": None, "inconclusive": True,
                        "judgment": attempt_judgment, "timeout": timeout_judgment,
                        "recovery": recovery_judgment}
            requested = row["body"]["request"].get("requested_seconds")
            if control_type == "timeout":
                requested = _require_duration(requested, "requested_seconds")
            else:
                requested = None
            # Recheck the current task/policy bindings in this transaction.  An
            # explicit authorization is a grant for a subsequent execution only.
            policy_row, policy = self._policy(row["project"])
            default = _require_duration(policy["default_task_timeout_seconds"], "default_task_timeout_seconds")
            if assessment is None:
                assessment = self.s.one("SELECT * FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                                        (row["task"], target["epoch"]))
            auth_id = None
            auth_body = None
            effective = None
            if control_type == "timeout" and timeout_judgment == "approved":
                effective = requested
            elif control_type == "recovery" and recovery_judgment == "approved":
                effective = None
            if effective is not None or (control_type == "recovery" and recovery_judgment == "approved"):
                control_revision = (self.s.one("SELECT COALESCE(MAX(control_revision),0) AS n FROM execution_control_authorizations WHERE task=?",
                                               (row["task"],))["n"] or 0) + 1
                auth_id = uid("XAUTH")
                auth_body = {"format": "daikibo.execution-control-authorization.v1", "control_type": control_type,
                             "proposal": proposal, "proposal_digest": row["digest"], "task": row["task"],
                             "task_revision": task["revision"], "control_revision": control_revision,
                             "target_attempt": {"epoch": target["epoch"], "ordinal": target.get("ordinal"),
                                                 "revision": target.get("revision"),
                                                 "binding": target.get("binding"),
                                                 "run": target.get("implementer_run"),
                                                 "receipt": target.get("implementer_receipt"),
                                                 "claim_only": bool(target.get("claim_only")),
                                                 "legacy": bool(target.get("legacy"))},
                             "requested_seconds": requested, "effective_seconds": effective,
                             "assessment": assessment["judgment"] if assessment else None,
                             "reviewer_run": review["run"], "reviewer_receipt": review_receipt,
                             "assessment_id": assessment["id"] if assessment else None,
                             "policy_revision": policy_row["revision"], "policy_digest": policy_row["digest"],
                             "default_seconds": default if control_type == "timeout" else None,
                             "semantic_digest": material.get("semantic_digest"),
                             "applies_to_subsequent_execution": control_type == "timeout"}
                self.s.execute("""INSERT INTO execution_control_authorizations
                    (id,proposal,project,task,task_revision,control_revision,proposal_digest,requested_seconds,effective_seconds,
                     assessment,reviewer_run,reviewer_receipt,body,digest,created)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (auth_id, proposal, row["project"], row["task"], task["revision"], control_revision,
                                row["digest"], requested, effective,
                                auth_body["assessment"], review["run"], review_receipt,
                                canonical(auth_body).decode(), digest(auth_body), timestamp()))
            fresh_task = self._task(actor, row["task"])
            result = {"proposal": proposal, "proposal_digest": row["digest"], "authorization": auth_id,
                      "authorization_digest": digest(auth_body) if auth_body else None,
                      "assessment": assessment["id"] if assessment else None,
                      "judgment": attempt_judgment, "timeout": timeout_judgment, "recovery": recovery_judgment,
                      "effective_seconds": effective, "no_progress_count": fresh_task["no_progress_count"],
                      "blocked": fresh_task["no_progress_count"] >= self._threshold(fresh_task),
                      "review_receipt": review_receipt}
            event_body = {"format": "daikibo.execution-control-event.v1", "proposal": proposal,
                          "proposal_digest": row["digest"], "kind": "applied", "result": result,
                          "reviewer_receipt": review_receipt,
                          "dispositions": {"attempt": attempt_judgment, "timeout": timeout_judgment,
                                           "recovery": recovery_judgment}}
            event_id = uid("XCEVENT")
            self.s.execute("INSERT INTO execution_control_events VALUES(?,?,?,?,?,?,?)",
                           (event_id, proposal, row["project"], "applied", canonical(event_body).decode(), digest(event_body), timestamp()))
            self.s.execute("UPDATE execution_control_proposals SET status='applied',result=? WHERE id=?",
                           (canonical({**result, "event": event_id}).decode(), proposal))
            self.sec.event(row["project"], "execution_control_applied", actor.id,
                           {"proposal": proposal, "authorization": auth_id, "assessment": assessment["id"] if assessment else None,
                            "judgment": attempt_judgment, "requested_seconds": requested})
            return {**result, "event": event_id, "replayed": False}

    def withdraw(self, actor, proposal, expected_digest, reason):
        with self.s.transaction():
            row = self._proposal_row(actor, proposal)
            actor.require("owner", "agent", project=row["project"], task=row["task"])
            text(reason, "withdrawal reason", 20_000)
            need(row["digest"] == expected_digest, "stale_digest", "Execution-control proposal digest differs")
            if row["status"] == "withdrawn":
                return {"proposal": proposal, "status": "withdrawn", "replayed": True}
            need(row["status"] == "proposed", "invalid_state", "Applied proposals need a new control proposal")
            body = {"format": "daikibo.execution-control-event.v1", "proposal": proposal,
                    "proposal_digest": expected_digest, "kind": "withdrawn", "reason": reason}
            ident = uid("XCEVENT")
            self.s.execute("INSERT INTO execution_control_events VALUES(?,?,?,?,?,?,?)",
                           (ident, proposal, row["project"], "withdrawn", canonical(body).decode(), digest(body), timestamp()))
            self.s.execute("UPDATE execution_control_proposals SET status='withdrawn' WHERE id=?", (proposal,))
            self.sec.event(row["project"], "execution_control_withdrawn", actor.id, {"proposal": proposal, "reason": reason})
            return {"proposal": proposal, "status": "withdrawn", "event": ident, "replayed": False}

    # ---------- timeout resolution and progress surfaces ----------

    def current_authorization(self, actor, task, requested=None, revision=None):
        row = self._task(actor, task)
        requested_value = _require_duration(requested, "requested_seconds") if requested is not None else None
        auths = self.s.all("SELECT * FROM execution_control_authorizations WHERE task=? AND task_revision=? ORDER BY control_revision DESC,created DESC",
                           (task, revision or row["revision"]))
        for auth in auths:
            body = parse_json(auth["body"])
            if body.get("control_type", "timeout") != "timeout":
                continue
            if body.get("proposal_digest") != auth["proposal_digest"] or digest(body) != auth["digest"]:
                continue
            # The target task revision and semantic material are checked again
            # at use time.  Epoch/candidate/lease changes are intentionally not
            # in this digest; the authorization survives its own claim and
            # candidate collection.
            if body.get("semantic_digest"):
                target = body.get("target_attempt", {})
                try:
                    current = self._current_material(actor, task, {"attempt_epoch": target.get("epoch"),
                                                                    "attempt_ordinal": target.get("ordinal"),
                                                                    "task_revision": target.get("revision"),
                                                                    "implementer_run": target.get("run",
                                                                                                  target.get("implementer_run"))})
                except Fault:
                    continue
                if current.get("semantic_digest") != body.get("semantic_digest"):
                    continue
            if requested_value is None or auth["effective_seconds"] >= requested_value:
                return {"id": auth["id"], "digest": auth["digest"], "effective_seconds": auth["effective_seconds"],
                        "task_revision": auth["task_revision"], "proposal": auth["proposal"], "assessment": auth["assessment"]}
        return None

    def resolve_timeout(self, actor, task, requested=None):
        row = self._task(actor, task)
        _policy_row, policy = self._policy(row["project"])
        default = _require_duration(policy["default_task_timeout_seconds"], "default_task_timeout_seconds")
        requested_value = _require_duration(requested, "task timeout") if requested is not None else None
        authorization = self.current_authorization(actor, task, requested_value, row["revision"])
        if authorization is not None:
            effective = _require_duration(authorization["effective_seconds"], "authorized task timeout")
            # A reviewed control grants the requested duration to the next
            # execution of this exact revision, including when the Task body
            # omitted timeout (or retained its older shorter timeout).
            if requested_value is None or effective >= requested_value:
                return {"seconds": effective, "default_seconds": default,
                        "authorization": authorization, "policy_revision": _policy_row["revision"]}
        if requested_value is None:
            requested_value = default
        if requested_value > default:
            need(False, "timeout_authorization_required",
                 "A task timeout above the reviewed policy default needs current execution-control authorization")
        return {"seconds": requested_value, "default_seconds": default,
                "authorization": None, "policy_revision": _policy_row["revision"]}

    def resolve_check_timeout(self, actor, task, requested=None):
        value = 300.0 if requested is None else _require_duration(requested, "check timeout")
        resolved = self.resolve_timeout(actor, task, value) if value > self._policy(self._task(actor, task)["project"])[1]["default_task_timeout_seconds"] else {"seconds": value, "authorization": None}
        return {"seconds": resolved["seconds"], "authorization": resolved.get("authorization"), "check_default_seconds": 300.0}

    def history(self, actor, task, offset=0, limit=100, expected_snapshot=None):
        row = self._task(actor, task)
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             "invalid_range", "Use a bounded execution-control history page")
        attempts = self.s.all("SELECT id,task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,status,implementer_run,implementer_receipt,digest,created,updated FROM execution_attempts WHERE task=? ORDER BY attempt_epoch", (task,))
        # Migration never creates guessed rows.  Expose retained legacy runs
        # and claim-only events as virtual history records so callers can select
        # them for review without turning them into live database rows.
        epochs = {value["attempt_epoch"] for value in attempts}
        legacy_epochs = set()
        for value in self.s.all("SELECT epoch FROM runs WHERE task=? AND role='implementer' AND epoch IS NOT NULL", (task,)):
            legacy_epochs.add(value["epoch"])
        for value in self.s.all("SELECT body FROM events WHERE project=? AND kind='task_claimed'", (row["project"],)):
            event_body = parse_json(value["body"])
            if event_body.get("task") == task and type(event_body.get("epoch")) is int:
                legacy_epochs.add(event_body["epoch"])
        for epoch in sorted(legacy_epochs - epochs):
            target = self._legacy_target(actor, task, epoch)
            if target is not None:
                attempts.append({"id": target["id"], "task": task, "project": row["project"],
                                 "attempt_epoch": epoch, "attempt_ordinal": target.get("attempt_ordinal"),
                                 "task_revision": target.get("task_revision"), "task_binding": target.get("task_binding"),
                                 "status": target["status"], "implementer_run": target.get("implementer_run"),
                                 "implementer_receipt": target.get("implementer_receipt"),
                                 "digest": digest(target["body"]), "created": None, "updated": None,
                                 "legacy": True, "body": target["body"]})
        attempts.sort(key=lambda value: (value["attempt_epoch"], value.get("attempt_ordinal") is None,
                                         value.get("attempt_ordinal") or 0, value["id"]))
        assessments = self.s.all("SELECT id,task,project,attempt_epoch,attempt_ordinal,task_revision,task_binding,proposal,proposal_digest,implementer_run,implementer_receipt,reviewer_run,reviewer_receipt,judgment,rationale,digest,created FROM attempt_assessments WHERE task=? ORDER BY attempt_ordinal", (task,))
        proposals = self.s.all("SELECT id,task,project,task_revision,digest,binding,status,created FROM execution_control_proposals WHERE task=? ORDER BY created,id", (task,))
        authorizations = self.s.all("SELECT id,proposal,task_revision,control_revision,proposal_digest,requested_seconds,effective_seconds,assessment,reviewer_run,reviewer_receipt,digest,created FROM execution_control_authorizations WHERE task=? ORDER BY control_revision", (task,))
        events = self.s.all("SELECT id,proposal,project,kind,digest,created FROM execution_control_events WHERE project=? AND proposal IN (SELECT id FROM execution_control_proposals WHERE task=?) ORDER BY created,id", (row["project"], task))
        value = {"attempts": attempts, "assessments": assessments, "proposals": proposals, "authorizations": authorizations, "events": events}
        stamp = digest(value)
        need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
             "stale_history", "Execution-control history changed between pages")
        # The complete value is retained in the response while the top-level
        # page is deterministic for callers that need bounded claim rows.
        page = attempts[offset:offset + limit]
        return {"task": task, "project": row["project"], "revision": row["revision"],
                "attempts": page, "assessments": assessments, "proposals": proposals,
                "authorizations": authorizations, "events": events, "snapshot": stamp,
                "total": len(attempts), "next_offset": offset + limit if offset + limit < len(attempts) else None,
                "no_progress_count": row["no_progress_count"], "legacy_attempts": row["attempts"]}

    # ---------- read-only progress reporting ----------

    @staticmethod
    def _progress_json(value):
        try:
            return parse_json(value)
        except Fault:
            return None

    @staticmethod
    def _progress_int(values):
        """Return one proven positive integer, or null for unknown/conflict."""
        values = [value for value in values if value is not None]
        invalid = any(type(value) is not int or value <= 0 for value in values)
        known = set(value for value in values if type(value) is int and value > 0)
        return (next(iter(known)) if len(known) == 1 and not invalid else None,
                invalid or len(known) > 1)

    def _progress_claim_events(self, task, project, epoch, index=None):
        if index is not None:
            return [event for event in index.get(task, [])
                    if epoch is None or event["body"].get("epoch") == epoch]
        events = []
        for row in self.s.all("SELECT id,body,seq FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (project,)):
            body = self._progress_json(row["body"])
            if isinstance(body, dict) and body.get("task") == task and (epoch is None or body.get("epoch") == epoch):
                events.append({"id": row["id"], "body": body, "seq": row["seq"]})
        return events

    def _progress_epochs(self, task, project, claim_events=None):
        epochs = set()
        for row in self.s.all("SELECT attempt_epoch FROM execution_attempts WHERE task=?", (task,)):
            if type(row["attempt_epoch"]) is int:
                epochs.add(row["attempt_epoch"])
        for row in self.s.all("SELECT epoch FROM runs WHERE task=? AND role='implementer' AND epoch IS NOT NULL", (task,)):
            if type(row["epoch"]) is int:
                epochs.add(row["epoch"])
        if claim_events is None:
            claim_events = self._progress_claim_events(task, project, None)
        for event in claim_events:
            if type(event["body"].get("epoch")) is int:
                epochs.add(event["body"]["epoch"])
        return epochs

    def _progress_claim_index(self, project):
        """Read claim event bodies once for a project page."""
        index = {}
        for row in self.s.all("SELECT id,body,seq FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (project,)):
            body = self._progress_json(row["body"])
            if isinstance(body, dict) and isinstance(body.get("task"), str):
                index.setdefault(body["task"], []).append({"id": row["id"], "body": body, "seq": row["seq"]})
        return index

    @staticmethod
    def _progress_detail_pointer(task, epoch, kind):
        return {"route": "execution_control.history_detail", "task": task, "attempt_epoch": epoch,
                "kind": kind, "offset": 0, "limit": PROGRESS_REF_LIMIT}

    def _progress_refs(self, values, total, task, epoch, kind):
        values = sorted(set(values))[:PROGRESS_REF_LIMIT]
        truncated = total > len(values)
        return values, total, truncated, self._progress_detail_pointer(task, epoch, kind) if truncated else None

    def _progress_stream_rows(self, sql, args=(), batch=128):
        """Yield compact SQL rows in bounded batches inside the read transaction."""
        cursor = self.s.conn.execute(sql, args)
        try:
            while True:
                rows = cursor.fetchmany(batch)
                if not rows:
                    return
                for row in rows:
                    yield dict(row)
        finally:
            cursor.close()

    @staticmethod
    def _progress_stamp_add(hasher, section, row):
        encoded = canonical({"section": section, "row": row})
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)

    def _progress_evidence_stamp(self, project):
        """Digest bounded identities/status columns without materializing evidence bodies."""
        project_row = self.s.one("SELECT id,paused,config FROM projects WHERE id=?", (project,), True)
        hasher = hashlib.sha256()
        self._progress_stamp_add(
            hasher, "format", {"name": "daikibo.project-progress-stamp.v2", "project": project})
        self._progress_stamp_add(
            hasher, "project", {"id": project_row["id"], "paused": project_row["paused"],
                                 "config_digest": digest(project_row["config"])})
        # These projections intentionally use only compact mutable state and
        # immutable row identities/digests.  Detailed JSON bodies are read by
        # the requested task page, never by the page-wide stamp.
        streams = {
            "tasks": ("SELECT id,revision,epoch,status,validity,paused,attempts,no_progress_count,updated,candidate "
                       "FROM tasks WHERE project=? ORDER BY created,id", (project,)),
            "runs": ("SELECT id,task,role,epoch,status,binding,start,end FROM runs "
                     "WHERE project=? ORDER BY id", (project,)),
            "receipts": ("SELECT id,run,subject,role,binding,created,key_id,mac FROM receipts "
                         "WHERE project=? ORDER BY id", (project,)),
            "events": ("SELECT seq,id,kind,created,mac FROM events WHERE project=? ORDER BY seq", (project,)),
            "candidates": ("SELECT id,task,epoch,digest,implementation_run,created FROM candidates "
                           "WHERE task IN (SELECT id FROM tasks WHERE project=?) ORDER BY id", (project,)),
            "attempts": ("SELECT id,task,attempt_epoch,attempt_ordinal,task_revision,status,implementer_run,"
                         "implementer_receipt,updated,digest FROM execution_attempts WHERE project=? ORDER BY id", (project,)),
            "assessments": ("SELECT id,task,attempt_epoch,attempt_ordinal,judgment,digest,created "
                            "FROM attempt_assessments WHERE project=? ORDER BY id", (project,)),
            "proposals": ("SELECT id,task,task_revision,digest,status,created FROM execution_control_proposals "
                          "WHERE project=? ORDER BY id", (project,)),
            "authorizations": ("SELECT id,task,task_revision,control_revision,proposal_digest,effective_seconds,"
                               "assessment,digest,created FROM execution_control_authorizations WHERE project=? ORDER BY id", (project,)),
            "control_events": ("SELECT id,proposal,kind,digest,created FROM execution_control_events "
                               "WHERE project=? ORDER BY id", (project,)),
            "blocks": ("SELECT task,kind,ref,reason FROM blocks WHERE task IN "
                       "(SELECT id FROM tasks WHERE project=?) ORDER BY task,kind,ref", (project,)),
            "policies": ("SELECT project,revision,digest FROM policies WHERE project=?", (project,)),
        }
        for section, (sql, args) in streams.items():
            self._progress_stamp_add(hasher, section, {"begin": True})
            for row in self._progress_stream_rows(sql, args):
                self._progress_stamp_add(hasher, section, row)
        return hasher.hexdigest()

    def history_detail(self, actor, task, attempt_epoch, kind, offset=0, limit=16, expected_snapshot=None):
        with self.s.transaction():
            return self._history_detail_locked(actor, task, attempt_epoch, kind, offset, limit, expected_snapshot)

    def _history_detail_locked(self, actor, task, attempt_epoch, kind, offset=0, limit=16, expected_snapshot=None):
        """Read a bounded page of retained raw run/receipt identities.

        This deliberately bypasses legacy-target reconciliation: an ambiguous
        epoch is exactly the evidence this diagnostic reader must expose, and
        it never creates an attempt or grants admission.
        """
        row = self._task(actor, task)
        need(type(attempt_epoch) is int and attempt_epoch >= 0,
             "invalid_epoch", "Attempt epoch must be a nonnegative integer")
        need(kind in {"implementer_runs", "implementer_receipts"},
             "invalid_kind", "Use implementer_runs or implementer_receipts")
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             "invalid_range", "Use a bounded execution-control detail page")
        if kind == "implementer_runs":
            total_sql = "SELECT count(*) AS n FROM runs WHERE task=? AND role='implementer' AND epoch=?"
            page_sql = ("SELECT id,project,task,subject,role,adapter,status,binding,epoch,worker_uid,pid,start,end "
                        "FROM runs WHERE task=? AND role='implementer' AND epoch=? ORDER BY id LIMIT ? OFFSET ?")
        else:
            total_sql = ("SELECT count(*) AS n FROM receipts q JOIN runs r ON r.id=q.run "
                         "WHERE r.task=? AND r.role='implementer' AND r.epoch=?")
            page_sql = ("SELECT q.id,q.run,q.project,q.subject,q.role,q.binding,q.created,q.key_id,q.mac "
                        "FROM receipts q JOIN runs r ON r.id=q.run "
                        "WHERE r.task=? AND r.role='implementer' AND r.epoch=? ORDER BY q.id LIMIT ? OFFSET ?")
        stamp = self._history_detail_stamp(task, attempt_epoch, kind, row["project"])
        need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
             "stale_history_detail", "Execution-control detail changed between pages")
        total = self.s.one(total_sql, (task, attempt_epoch))["n"]
        records = self.s.all(page_sql, (task, attempt_epoch, limit, offset))
        return {"task": task, "project": row["project"], "attempt_epoch": attempt_epoch,
                "kind": kind, "records": records, "offset": offset, "limit": limit,
                "snapshot": stamp, "total": total,
                "next_offset": offset + limit if offset + limit < total else None}

    def _history_detail_stamp(self, task, attempt_epoch, kind, project):
        hasher = hashlib.sha256()
        self._progress_stamp_add(hasher, "format", {"name": "daikibo.execution-control-detail.v1",
                                                     "task": task, "project": project,
                                                     "attempt_epoch": attempt_epoch, "kind": kind})
        if kind == "implementer_runs":
            sql = ("SELECT id,project,task,subject,role,adapter,status,binding,epoch,worker_uid,pid,start,end "
                   "FROM runs WHERE task=? AND role='implementer' AND epoch=? ORDER BY id")
        else:
            sql = ("SELECT q.id,q.run,q.project,q.subject,q.role,q.binding,q.created,q.key_id,q.mac "
                   "FROM receipts q JOIN runs r ON r.id=q.run "
                   "WHERE r.task=? AND r.role='implementer' AND r.epoch=? ORDER BY q.id")
        for row in self._progress_stream_rows(sql, (task, attempt_epoch)):
            self._progress_stamp_add(hasher, kind, row)
        return hasher.hexdigest()

    def _progress_epoch(self, actor, task, row, epoch, claim_index=None):
        """Collect bounded, non-authoritative evidence for one observed epoch."""
        attempt = self.s.one("SELECT * FROM execution_attempts WHERE task=? AND attempt_epoch=?", (task, epoch))
        attempt_body = self._progress_json(attempt["body"]) if attempt else None
        if attempt_body is not None and digest(attempt_body) != attempt.get("digest"):
            attempt_body = None

        claim_events = self._progress_claim_events(task, row["project"], epoch, claim_index)
        run_total = self.s.one(
            "SELECT count(*) AS n FROM runs WHERE task=? AND role='implementer' AND epoch=?", (task, epoch))["n"]
        runs = self.s.all(
            "SELECT id,status,binding,epoch,start,end,body FROM runs "
            "WHERE task=? AND role='implementer' AND epoch=? ORDER BY id LIMIT ?",
            (task, epoch, PROGRESS_REF_LIMIT),
        )
        receipt_total = self.s.one(
            "SELECT count(*) AS n FROM receipts q JOIN runs r ON r.id=q.run "
            "WHERE r.task=? AND r.role='implementer' AND r.epoch=?", (task, epoch))["n"]
        receipt_rows = self.s.all(
            "SELECT q.id,q.run,q.body FROM receipts q JOIN runs r ON r.id=q.run "
            "WHERE r.task=? AND r.role='implementer' AND r.epoch=? ORDER BY q.id LIMIT ?",
            (task, epoch, PROGRESS_REF_LIMIT),
        )
        receipt_bodies = {}
        if run_total == 1 and receipt_total == 1 and receipt_rows:
            try:
                receipt_bodies[receipt_rows[0]["id"]] = self.g.receipt(receipt_rows[0]["id"])
            except Fault:
                receipt_bodies[receipt_rows[0]["id"]] = None
        if attempt and attempt.get("implementer_receipt"):
            receipt_id = attempt["implementer_receipt"]
            if receipt_id not in {item["id"] for item in receipt_rows}:
                receipt_total = max(receipt_total, 1)
            if receipt_id not in receipt_bodies:
                try:
                    receipt_bodies[receipt_id] = self.g.receipt(receipt_id)
                except Fault:
                    receipt_bodies[receipt_id] = None

        ordinal_values = []
        revision_values = []
        if attempt:
            ordinal_values.append(attempt.get("attempt_ordinal"))
            revision_values.append(attempt.get("task_revision"))
            if isinstance(attempt_body, dict):
                ordinal_values.append(attempt_body.get("ordinal", attempt_body.get("attempt_ordinal")))
                revision_values.append(attempt_body.get("revision", attempt_body.get("task_revision")))
        for event in claim_events:
            ordinal_values.append(event["body"].get("attempt_ordinal", event["body"].get("ordinal")))
            revision_values.append(event["body"].get("task_revision"))
        for run in runs:
            body = self._progress_json(run["body"])
            if isinstance(body, dict):
                ordinal_values.append(body.get("attempt_ordinal", body.get("ordinal")))
                revision_values.append(body.get("task_revision", body.get("revision")))
        for receipt in receipt_bodies.values():
            if isinstance(receipt, dict):
                ordinal_values.append(receipt.get("attempt_ordinal", receipt.get("ordinal")))
                revision_values.append(receipt.get("task_revision", receipt.get("revision")))
        ordinal, ordinal_ambiguous = self._progress_int(ordinal_values)
        revision, revision_ambiguous = self._progress_int(revision_values)
        run_ids, run_total, run_truncated, run_detail = self._progress_refs(
            [run["id"] for run in runs], run_total, task, epoch, "implementer_runs")
        receipt_ids, receipt_total, receipt_truncated, receipt_detail = self._progress_refs(
            [receipt["id"] for receipt in receipt_rows], receipt_total, task, epoch, "implementer_receipts")
        if attempt and attempt.get("implementer_receipt") and attempt["implementer_receipt"] not in receipt_ids:
            receipt_ids, receipt_total, receipt_truncated, receipt_detail = self._progress_refs(
                receipt_ids + [attempt["implementer_receipt"]], receipt_total, task, epoch, "implementer_receipts")

        valid_receipts = []
        if run_total == 1 and runs and receipt_total == 1:
            for receipt_id, receipt in receipt_bodies.items():
                if (isinstance(receipt, dict) and receipt.get("run") == runs[0]["id"]
                        and receipt.get("subject") == task and receipt.get("task") == task
                        and receipt.get("role") == "implementer" and receipt.get("epoch") == epoch):
                    valid_receipts.append(receipt)
        if run_total > 1:
            observation = "ambiguous"
            outcome = "ambiguous"
            evidence_complete = False
        elif run_total == 0:
            observation = "unobserved"
            outcome = "unknown"
            evidence_complete = False
        else:
            run = runs[0]
            terminal = run.get("status") == "finished" or run.get("end") is not None
            observation = "terminal" if terminal else "running"
            evidence_complete = terminal and len(valid_receipts) == 1
            if not evidence_complete:
                outcome = "unknown" if terminal else "running"
            else:
                receipt = valid_receipts[0]
                result = receipt.get("result") or {}
                if receipt.get("cancelled"):
                    outcome = "cancelled"
                elif (receipt.get("exit_code") == 0
                      and not any(receipt.get(key) for key in ("timed_out", "output_overflow", "failure"))
                      and not result.get("error") and not result.get("collector_error")):
                    outcome = "succeeded"
                else:
                    outcome = "failed"
        if ordinal_ambiguous or revision_ambiguous:
            if run_total <= 1 and run_total:
                observation = "ambiguous"
            ordinal = None if ordinal_ambiguous else ordinal
            revision = None if revision_ambiguous else revision

        return {
            "epoch": epoch,
            "ordinal": ordinal,
            "task_revision": revision,
            "observation": observation,
            "evidence_complete": evidence_complete,
            "implementer": {"run_ids": run_ids, "receipt_ids": receipt_ids, "outcome": outcome,
                             "run_total": run_total, "run_truncated": run_truncated, "run_detail": run_detail,
                             "receipt_total": receipt_total, "receipt_truncated": receipt_truncated,
                             "receipt_detail": receipt_detail},
            "claim_record": attempt is not None,
            "claim_event": bool(claim_events),
            "run_evidence": run_total > 0,
        }

    def _progress_review_roles(self, row):
        policy_row = self.s.one("SELECT body FROM policies WHERE project=?", (row["project"],))
        if policy_row:
            body = self._progress_json(policy_row["body"]) or {}
        else:
            body = {}
        roles = set(body.get("review_roles", ("spec", "quality", "test_adequacy")))
        if row["body"].get("risk") == "critical":
            roles.update(body.get("critical_review_roles", ("specialist",)))
        return sorted(role for role in roles if isinstance(role, str))

    def _progress_review_entry(self, actor, task, epoch, binding, row):
        receipt_id = row["id"]
        try:
            receipt = self.g.receipt(receipt_id)
        except Fault:
            raw = self._progress_json(row.get("body"))
            if isinstance(raw, dict) and (raw.get("failure") or raw.get("exit_code") not in (None, 0)):
                observation = "execution_failed"
            else:
                observation = "invalid_or_unknown"
            return {"role": row["role"], "run": row["run"], "receipt": receipt_id,
                    "binding": binding, "observation": observation}
        if (receipt.get("subject") != task or receipt.get("task") != task
                or receipt.get("role") != row["role"] or receipt.get("binding") != binding
                or receipt.get("epoch") != epoch):
            observation = "invalid_or_unknown"
        elif (receipt.get("exit_code") != 0
              or any(receipt.get(key) for key in ("timed_out", "cancelled", "output_overflow", "failure", "collector_error"))):
            observation = "execution_failed"
        elif receipt.get("readonly_verified") is not True or receipt.get("judgment_valid") is not True:
            observation = "invalid_or_unknown"
        else:
            result = receipt.get("result") or {}
            verdict = result.get("verdict")
            if verdict in {"fail", "blocked"}:
                observation = verdict
            elif verdict == "pass" and not result.get("findings"):
                try:
                    self.g.require_review(receipt_id, task, binding, {row["role"]})
                except Fault:
                    observation = "invalid_or_unknown"
                else:
                    observation = "pass"
            else:
                observation = "invalid_or_unknown"
        return {"role": row["role"], "run": row["run"], "receipt": receipt_id,
                "binding": binding, "observation": observation}

    def _progress_reviews(self, actor, task, epoch, row):
        roles = self._progress_review_roles(row)
        if not roles:
            return {"state": "none", "latest_by_role": []}
        placeholders = ",".join("?" for _ in roles)
        rows = self.s.all(
            "SELECT q.id,q.run,q.role,q.binding,q.body,q.created,r.epoch,r.task "
            "FROM receipts q JOIN runs r ON r.id=q.run "
            f"WHERE q.subject=? AND r.task=? AND r.epoch=? AND q.role IN ({placeholders}) "
            "ORDER BY q.created,q.id",
            (task, task, epoch, *roles),
        )
        if not rows:
            return {"state": "none", "latest_by_role": []}
        groups = {}
        for item in rows:
            groups.setdefault(item["binding"], []).append(item)

        selected_binding = None
        if row["epoch"] == epoch and row.get("candidate"):
            candidate = self.s.one("SELECT epoch FROM candidates WHERE id=?", (row["candidate"],))
            if candidate and candidate["epoch"] == epoch:
                try:
                    current_binding = self.g.task_binding(task, ensure_policy=False)
                except Fault:
                    current_binding = None
                if current_binding not in groups:
                    # A review from another candidate/input binding is not
                    # evidence for the current candidate, even when it is the
                    # only retained group for this epoch.
                    return {"state": "pending_or_incomplete", "latest_by_role": []}
                selected_binding = current_binding
        if selected_binding is None and len(groups) == 1:
            selected_binding = next(iter(groups))
        if selected_binding is None:
            return {"state": "pending_or_incomplete", "latest_by_role": []}

        latest = {}
        for item in groups[selected_binding]:
            latest[item["role"]] = item
        entries = [self._progress_review_entry(actor, task, epoch, selected_binding, latest[role])
                   for role in sorted(latest)]
        observations = {item["role"]: item["observation"] for item in entries}
        if any(value in {"fail", "blocked"} for value in observations.values()):
            state = "rejected"
        elif (set(observations) != set(roles)
              or any(value != "pass" for value in observations.values())):
            state = "pending_or_incomplete"
        else:
            state = "observed_passes"
        return {"state": state, "latest_by_role": entries}

    def _progress_current_claim(self, row, epoch_info):
        if epoch_info is None or not (epoch_info["claim_record"] or epoch_info["claim_event"] or epoch_info["run_evidence"]):
            return None
        if epoch_info["claim_record"]:
            source = "claim_record"
        elif epoch_info["claim_event"]:
            source = "retained_event"
        else:
            source = "retained_run"
        evidence = epoch_info["implementer"]
        return {"epoch": epoch_info["epoch"], "ordinal": epoch_info["ordinal"],
                "observation": epoch_info["observation"], "source": source,
                "run_ids": evidence["run_ids"], "receipt_ids": evidence["receipt_ids"],
                "run_total": evidence["run_total"], "run_truncated": evidence["run_truncated"],
                "run_detail": evidence["run_detail"], "receipt_total": evidence["receipt_total"],
                "receipt_truncated": evidence["receipt_truncated"], "receipt_detail": evidence["receipt_detail"]}

    def _progress_next_claim(self, row, project_paused, current_claim, latest, evidence_missing):
        reference = current_claim["epoch"] if current_claim else latest["epoch"] if latest else None
        paused = bool(project_paused or row["paused"])
        status = row["status"]
        if status == "running":
            observation = current_claim.get("observation") if current_claim else None
            if observation in {"terminal", "ambiguous"}:
                state = "reassess_current_result"
                code = "current_execution_evidence_ambiguous" if observation == "ambiguous" \
                    else "current_execution_observed_terminal"
            elif observation is None:
                state = "wait_for_current_execution"
                code = "current_claim_not_observed"
            else:
                state = "wait_for_current_execution"
                code = "current_claim_not_terminal"
        elif status == "submitted":
            state = "reassess_current_result"
            code = "submitted_result_before_next_claim" if not evidence_missing \
                else "legacy_attempt_details_unavailable"
        elif status == "ready":
            state = "evaluate_ready_gates"
            code = "legacy_attempt_details_unavailable" if evidence_missing else "ready_requires_existing_gates"
        elif status == "planned":
            state = "prepare_ready_state"
            code = "legacy_attempt_details_unavailable" if evidence_missing else "planned_requires_ready_state"
        elif status == "completed":
            state, code = "not_applicable", "completed"
        elif status == "cancelled":
            state, code = "not_applicable", "cancelled"
        else:
            state, code = "not_applicable", "legacy_attempt_details_unavailable" if evidence_missing else "not_applicable"
        result = {"state": state, "reference_attempt_epoch": reference,
                  "projected_authorization": False, "explanation_code": "paused" if paused else code}
        if paused:
            result["paused"] = True
            result["lifecycle_explanation_code"] = code
        return result

    def _progress_reporting_locked(self, actor, task, row=None, claim_index=None):
        row = row or self._task(actor, task)
        project = self.s.one("SELECT paused FROM projects WHERE id=?", (row["project"],), True)
        epochs = self._progress_epochs(task, row["project"], claim_index.get(task, []) if claim_index is not None else None)
        current_info = self._progress_epoch(actor, task, row, row["epoch"], claim_index) if row["epoch"] in epochs else None
        latest_epoch = max(epochs) if epochs else None
        latest_info = current_info if current_info is not None and latest_epoch == row["epoch"] \
            else self._progress_epoch(actor, task, row, latest_epoch, claim_index) if latest_epoch is not None else None
        current_claim = self._progress_current_claim(row, current_info)
        latest_attempt = None
        if latest_info is not None:
            latest_attempt = {
                "epoch": latest_info["epoch"], "ordinal": latest_info["ordinal"],
                "task_revision": latest_info["task_revision"], "observation": latest_info["observation"],
                "evidence_complete": latest_info["evidence_complete"],
                "implementer": latest_info["implementer"],
                "reviews": self._progress_reviews(actor, task, latest_info["epoch"], row),
                "assessment": None,
                "implementation_success_is_completion": False,
            }
            assessment = self.s.one(
                "SELECT id,digest,judgment FROM attempt_assessments WHERE task=? AND attempt_epoch=?",
                (task, latest_info["epoch"]),
            )
            if assessment:
                latest_attempt["assessment"] = {"id": assessment["id"], "digest": assessment["digest"],
                                                 "judgment": assessment["judgment"]}
        evidence_missing = row["attempts"] > 0 and latest_info is None
        context = {
            "task_revision": row["revision"], "task_epoch": row["epoch"], "task_status": row["status"],
            "validity": row["validity"], "paused": bool(row["paused"]),
            "project_paused": bool(project["paused"]),
            "admission_scope": "prior_epochs_of_current_task_epoch",
            "admission_is_next_claim_authorization": False,
        }
        projection = {
            "format": "daikibo.task-progress.v1", "context": context,
            "current_claim": current_claim, "latest_attempt": latest_attempt,
            "next_claim": self._progress_next_claim(row, bool(project["paused"]), current_claim,
                                                     latest_attempt, evidence_missing),
        }
        return {**projection, "snapshot": digest(projection)}

    def _progress_reporting(self, actor, task):
        with self.s.transaction():
            return self._progress_reporting_locked(actor, task)

    def _progress_admission(self, actor, task):
        """Preserve an ambiguous gate diagnostic without selecting evidence."""
        try:
            return self.admission(actor, task, readonly=True)
        except Fault as exc:
            if exc.code != "ambiguous_attempt":
                raise
            row = self._task(actor, task)
            try:
                failures = list(self.g.check_current(task, ensure_policy=False))
            except Fault:
                failures = []
            if exc.code not in failures:
                failures.append(exc.code)
            return {"allowed": False, "failures": failures,
                    "threshold": self._threshold(row, readonly=True),
                    "no_progress_count": row["no_progress_count"], "attempts": row["attempts"],
                    "legacy_attempt_limit_blocking": False,
                    "diagnostic": {"code": exc.code, "message": exc.message,
                                    "details": exc.details, "non_authorizing": True}}

    def progress(self, actor, task):
        with self.s.transaction():
            row = self._task(actor, task)
            admission = self._progress_admission(actor, task)
            try:
                history_snapshot = self.history(actor, task, 0, 200)["snapshot"]
            except Fault as exc:
                need(exc.code == "ambiguous_attempt", exc.code, exc.message, exc.details)
                history_snapshot = digest({"task": task, "ambiguous": True,
                                           "attempts": self.s.all("SELECT id,attempt_epoch,attempt_ordinal,implementer_run,implementer_receipt,digest FROM execution_attempts WHERE task=? ORDER BY attempt_epoch,id", (task,)),
                                           "runs": self.s.all("SELECT id,epoch,binding,status FROM runs WHERE task=? AND role='implementer' ORDER BY epoch,id", (task,)),
                                           "events": self.s.all("SELECT id,body FROM events WHERE project=? AND kind='task_claimed' ORDER BY seq", (row["project"],))})
            reporting = self._progress_reporting_locked(actor, task, row)
            return {"task": task, "project": row["project"], "revision": row["revision"], "epoch": row["epoch"],
                    "status": row["status"], "validity": row["validity"], "attempts": row["attempts"],
                    "no_progress_count": row["no_progress_count"], "threshold": admission["threshold"],
                    "telemetry": {"attempts": row["attempts"], "no_progress_count": row["no_progress_count"],
                                  "legacy_attempt_limit_blocking": False},
                    "admission": admission, "history_snapshot": history_snapshot,
                    "reporting": reporting}

    def project_progress(self, actor, project, offset=0, limit=100, expected_snapshot=None):
        with self.s.transaction():
            self.k.project(actor, project)
            need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
                 "invalid_range", "Use a bounded project progress page")
            stamp = self._progress_evidence_stamp(project)
            need((offset == 0 and expected_snapshot is None) or expected_snapshot == stamp,
                 "stale_progress", "Project progress changed between pages")
            totals = self.s.one(
                "SELECT count(*) AS total, COALESCE(sum(attempts),0) AS attempts, "
                "COALESCE(sum(no_progress_count),0) AS no_progress_count FROM tasks WHERE project=?", (project,))
            rows = self.s.all(
                "SELECT id,revision,epoch,status,validity,attempts,no_progress_count FROM tasks "
                "WHERE project=? ORDER BY created,id LIMIT ? OFFSET ?", (project, limit, offset))
            claim_index = self._progress_claim_index(project)
            items = []
            for row in rows:
                admission = self._progress_admission(actor, row["id"])
                reporting = self._progress_reporting_locked(actor, row["id"], claim_index=claim_index)
                items.append({**row, "threshold": admission["threshold"], "admission": admission,
                              "telemetry": {"attempts": row["attempts"], "no_progress_count": row["no_progress_count"],
                                            "legacy_attempt_limit_blocking": False},
                              "reporting": reporting})
            total = totals["total"]
            return {"project": project, "items": items, "snapshot": stamp, "total": total,
                    "next_offset": offset + limit if offset + limit < total else None,
                    "aggregate": {"tasks": total, "attempts": totals["attempts"],
                                   "no_progress_count": totals["no_progress_count"]}}
