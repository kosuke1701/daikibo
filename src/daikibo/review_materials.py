"""Canonical live material builders for review-sensitive transitions.

The returned binding is computed from the same canonical projection supplied
to the reviewer. Operational fields such as Task epochs and artifact creation
timestamps are intentionally excluded where they do not describe review
meaning.
"""
from __future__ import annotations

from .common import Fault, canonical, digest, need, parse_json, text
from .knowledge import RELATIONS
from .review_dependencies import accepted_invariants


EMPTY_SNAPSHOT = {
    "format": "snapshot.v1",
    "repos": {},
    "digest": digest({"repos": {}}),
}
MAX_FROZEN_SNAPSHOT_BYTES = 256 * 1024 * 1024


class ReviewMaterials:
    """Build role-specific review contexts and their exact live bindings."""

    def __init__(self, runtime):
        self.rt = runtime

    def _policy(self, project):
        value = self.rt.g.policy(project, create=False)
        return {"revision": value["revision"], "digest": value["digest"],
                "body": value["body"]}

    def latest_plan_freeze(self, task_row, plan_row):
        """Return the verified latest freeze event for this saved plan."""
        from .observed_receipts import _validated_events

        if not plan_row:
            return None
        try:
            plan_body = parse_json(plan_row["body"])
        except (Fault, KeyError, TypeError, ValueError):
            return None
        if digest(plan_body) != plan_row.get("digest"):
            return None
        events = _validated_events(self.rt)
        matches = [body for row, body in events
                   if row.get("project") == task_row.get("project") and
                   row.get("kind") == "test_plan_frozen" and
                   body.get("task") == task_row.get("id")]
        if not matches:
            return None
        latest = matches[-1]
        if latest.get("digest") != plan_row["digest"]:
            return None
        if "approved" in latest and latest["approved"] != plan_row.get("approved"):
            return None
        return latest

    @staticmethod
    def _schema_digest(role):
        from .review_contract import review_schema
        return digest(review_schema(role))

    def _binding(self, *, kind, project, subject, role, snapshot, material,
                 include_role=True):
        value = {
            "format": "daikibo.review-material.v1",
            "kind": kind,
            "project": project,
            "subject": subject,
            "snapshot_digest": snapshot["digest"],
            "material": material,
        }
        if include_role:
            value["role"] = role
            value["review_schema_digest"] = self._schema_digest(role)
        return digest(value)

    def _snapshot(self, actor, task_row, task_body, *, store_blobs):
        if task_row["candidate"]:
            candidate = self.rt.s.one(
                "SELECT body FROM candidates WHERE id=?", (task_row["candidate"],), True,
            )
            body = parse_json(candidate["body"])
            snapshot = body.get("snapshot")
            need(isinstance(snapshot, dict) and isinstance(snapshot.get("digest"), str),
                 "invalid_candidate", "Candidate snapshot identity is malformed")
            return snapshot
        repos = task_body.get("repos", [])
        if repos:
            return self.rt.sn.capture(actor, task_row["project"], repos, store_blobs=store_blobs)
        return dict(EMPTY_SNAPSHOT)

    @staticmethod
    def _artifact_semantics(artifact):
        # Status and created are lifecycle/telemetry. The current revision and
        # exact body digest carry the proposal's meaning.
        return {key: artifact[key] for key in ("id", "project", "kind", "revision", "digest", "body")}

    def _artifact_sources(self, actor, artifact):
        result = []
        for source in artifact["body"].get("source_refs", []):
            row = self.rt.s.one(
                "SELECT project,blob FROM sources WHERE id=?", (source,), True,
            )
            need(row["project"] == artifact["project"], "cross_project",
                 "Artifact source belongs to another project")
            content = self.rt.s.blob_get(row["blob"]).decode()
            result.append({"id": source, "content": content, "digest": row["blob"]})
        return result

    def _artifact_record(self, actor, ident):
        artifact = self.rt.k.artifact(actor, ident)
        return artifact, self._artifact_sources(actor, artifact)

    def receipt_binding(self, actor, observed, *, ensure_policy=True):
        """Rebuild the exact live material named by an observed review prompt.

        Artifact receipts may review either the generic proposal or one exact
        asserted link proposal. Task test-plan receipts are tied to the saved
        frozen baseline; other Task roles use the current Task binding.
        """
        need(isinstance(observed, dict), "invalid_evidence",
             "Observed review receipt is malformed")
        receipt_id = observed.get("id")
        receipt = self.rt.g.receipt(receipt_id)
        raw = self.rt.s.blob_get(receipt["input_blob"])
        prompt = parse_json(raw)
        need(isinstance(prompt, dict) and digest(prompt) == receipt.get("input_digest") and
             prompt.get("subject") == receipt.get("subject") and
             prompt.get("role") == receipt.get("role") and
             prompt.get("binding") == receipt.get("binding"),
             "invalid_evidence", "Review prompt differs from its observed receipt")
        subject, role = receipt["subject"], receipt["role"]
        context = prompt.get("context")
        need(isinstance(context, dict), "invalid_evidence",
             "Review prompt context is malformed")

        artifact_row = self.rt.s.one("SELECT id,project,digest FROM artifacts WHERE id=?", (subject,))
        if artifact_row:
            if role == "domain_responsibility":
                artifact = self.rt.k.artifact(actor, subject)
                return artifact["digest"]
            proposal = context.get("link_proposal")
            if proposal is not None:
                return self.artifact_link(actor, subject, role, proposal)["binding"]
            return self.artifact(actor, subject, role)["binding"]

        task_row = self.rt.s.one("SELECT * FROM tasks WHERE id=?", (subject,))
        if task_row:
            if role == "test_plan":
                plan = self.rt.s.one("SELECT * FROM plans WHERE task=?", (subject,))
                need(plan is not None, "stale_evidence",
                     "Test-plan review no longer has a saved plan")
                plan_body = parse_json(plan["body"])
                need(context.get("test_plan") == plan_body,
                     "stale_evidence", "Test-plan review names a different saved plan")
                snapshot = self.frozen_plan_snapshot(actor, task_row, plan)
                need(snapshot is not None, "stale_evidence",
                     "Frozen test-plan review baseline is unavailable or stale")
                review_snapshot = context.get("review_snapshot")
                need(isinstance(review_snapshot, dict) and
                     review_snapshot.get("digest") == snapshot.get("digest") and
                     review_snapshot.get("format") == snapshot.get("format") and
                     receipt.get("snapshot") == snapshot.get("digest"),
                     "stale_evidence", "Test-plan review used a different baseline snapshot")
                return self.test_plan(actor, task_row, plan_body,
                                      store_snapshot_blobs=False,
                                      snapshot_override=snapshot)["binding"]
            return self.rt.g.task_binding(subject, ensure_policy=ensure_policy)
        return None

    def artifact(self, actor, subject, role):
        """Material for a generic canonical artifact proposal review."""
        with self.rt.s.transaction():
            artifact, sources = self._artifact_record(actor, subject)
            project = artifact["project"]
            policy = self._policy(project)
            invariants = accepted_invariants(self.rt.s, project)
            # An artifact that is itself an accepted invariant becomes part
            # of the project-wide set as a consequence of the acceptance
            # transition. Its own review was made before that transition, and
            # comparing it with itself would immediately stale the receipt.
            # Keep this projection aligned with assurance_node_reviews.
            if artifact["body"].get("constraints") or artifact["body"].get("critical"):
                invariants = [item for item in invariants if item["id"] != subject]
            snapshot = dict(EMPTY_SNAPSHOT)
            context = {
                "artifact": artifact,
                "accepted_invariants": invariants,
                "sources": sources,
                "review_policy": policy,
                "review_snapshot": {"digest": snapshot["digest"], "format": snapshot["format"]},
            }
            material = {
                "artifact": self._artifact_semantics(artifact),
                "sources": sources,
                "accepted_invariants": invariants,
                "policy": policy,
            }
            return {"project": project, "binding": self._binding(
                        kind="artifact_proposal", project=project, subject=subject,
                        role=role, snapshot=snapshot, material=material, include_role=False),
                    "snapshot": snapshot, "context": context}

    def artifact_link(self, actor, source, role, proposal):
        """Material for one exact asserted trace edge proposal."""
        from .review_dependencies import accepted_invariants

        need(role in {"trace", "design"}, "invalid_role",
             "Asserted links require a trace or design review")
        need(isinstance(proposal, dict) and set(proposal) == {
            "format", "target", "relation", "confidence", "basis",
        }, "invalid_link_proposal", "Asserted link review needs one complete link proposal")
        need(proposal.get("format") == "artifact.link.v1" and
             proposal.get("confidence") == "asserted", "invalid_link_proposal",
             "Asserted link proposal format or confidence differs")
        text(proposal.get("target"), "link target", 200)
        need(proposal.get("relation") in RELATIONS and proposal.get("target") != source,
             "invalid_link_proposal", "Asserted link proposal relation or endpoints are invalid")
        text(proposal.get("basis"), "link basis", 12000)

        with self.rt.s.transaction():
            source_artifact, source_evidence = self._artifact_record(actor, source)
            target_artifact, target_evidence = self._artifact_record(actor, proposal["target"])
            project = source_artifact["project"]
            need(target_artifact["project"] == project, "cross_project",
                 "Trace cannot cross projects")
            policy = self._policy(project)
            invariants = accepted_invariants(self.rt.s, project)
            source_material = self._artifact_semantics(source_artifact) | {"status": source_artifact["status"]}
            target_material = self._artifact_semantics(target_artifact) | {"status": target_artifact["status"]}
            snapshot = dict(EMPTY_SNAPSHOT)
            sources = [
                {"artifact": source_material["id"], "sources": source_evidence},
                {"artifact": target_material["id"], "sources": target_evidence},
            ]
            context = {
                "link_proposal": proposal,
                "source_artifact": source_artifact,
                "target_artifact": target_artifact,
                "artifact_sources": sources,
                "accepted_invariants": invariants,
                "review_policy": policy,
                "review_snapshot": {"digest": snapshot["digest"], "format": snapshot["format"]},
            }
            material = {
                "proposal": proposal,
                "source_artifact": source_material,
                "target_artifact": target_material,
                "artifact_sources": sources,
                "accepted_invariants": invariants,
                "policy": policy,
            }
            return {"project": project, "binding": self._binding(
                        kind="asserted_artifact_link", project=project, subject=source_material["id"],
                        role=role, snapshot=snapshot, material=material),
                    "snapshot": snapshot, "context": context}

    def test_plan(self, actor, task_row, proposal, *, store_snapshot_blobs=True,
                  snapshot_override=None):
        """Material for a plan review, excluding volatile Task execution state."""
        from .obligations import review_task

        with self.rt.s.transaction():
            current = self.rt.s.one("SELECT * FROM tasks WHERE id=?", (task_row["id"],), True)
            current_body = parse_json(current["body"])
            passed_body = (task_row["body"] if isinstance(task_row.get("body"), dict)
                           else parse_json(task_row["body"]))
            need(current["revision"] == task_row["revision"] and current_body == passed_body,
                 "stale_review_material", "Task definition changed while building its review material")
            project = current["project"]
            task_body = current_body
            if proposal is None:
                saved = self.rt.s.one("SELECT body FROM plans WHERE task=?", (task_row["id"],))
                proposal = parse_json(saved["body"]) if saved else None
            need(proposal is None or isinstance(proposal, dict), "invalid_test_plan",
                 "Test plan proposal must be an object")

            read_artifacts = [self.rt.k.artifact(actor, ident)
                              for ident in task_body["read_artifacts"]]
            read_pins = self.rt.s.all(
                "SELECT artifact,revision,digest FROM task_reads WHERE task=? ORDER BY artifact",
                (task_row["id"],),
            )
            need({item["artifact"] for item in read_pins} == set(task_body["read_artifacts"]),
                 "stale_review_material", "Task read pins differ from its definition")
            pin_by_id = {item["artifact"]: item for item in read_pins}
            for artifact in read_artifacts:
                pin = pin_by_id[artifact["id"]]
                need(artifact["revision"] == pin["revision"] and artifact["digest"] == pin["digest"],
                     "stale_input", "Task read artifact changed; revise the Task before reviewing its plan",
                     artifact["id"])
            dependencies = self.rt.s.all(
                "SELECT dependency FROM task_deps WHERE task=? ORDER BY dependency",
                (task_row["id"],),
            )
            policy = self._policy(project)
            snapshot = (snapshot_override if snapshot_override is not None else
                        self._snapshot(actor, current, task_body, store_blobs=store_snapshot_blobs))
            need(isinstance(snapshot, dict) and isinstance(snapshot.get("digest"), str),
                 "invalid_review_material", "Test-plan baseline snapshot identity is malformed")
            task_view = review_task(self.rt.s, task_body)
            context = {
                "task": task_view,
                "read_artifacts": read_artifacts,
                "test_plan": proposal,
                "review_policy": policy,
                "review_snapshot": {"digest": snapshot["digest"], "format": snapshot.get("format")},
                "task_reads": read_pins,
                "dependencies": dependencies,
            }
            material = {
                "task": {"id": current["id"], "revision": current["revision"],
                         "definition": task_body, "review_view": task_view},
                "task_reads": read_pins,
                "read_artifacts": [self._artifact_semantics(item) | {"status": item["status"]}
                                   for item in read_artifacts],
                "dependencies": dependencies,
                "proposal": proposal,
                "snapshot_digest": snapshot["digest"],
                "policy": policy,
            }
            return {"project": project, "binding": self._binding(
                        kind="task_test_plan", project=project, subject=current["id"],
                        role="test_plan", snapshot=snapshot, material=material),
                    "snapshot": snapshot, "context": context}

    def _valid_full_snapshot(self, snapshot):
        if snapshot == EMPTY_SNAPSHOT:
            return True
        try:
            from .verification_materials import validate_sealed_snapshot
            validate_sealed_snapshot(self.rt.s, snapshot)
            return True
        except (Fault, KeyError, TypeError, ValueError):
            return False

    def frozen_plan_snapshot(self, actor, task_row, plan_row, *, require_manifest=False):
        """Retain the baseline snapshot used by a still-current frozen plan review.

        Once adopted, implementation changes to the repository are expected.
        The verified freeze event pins the review binding and baseline
        digest, so the current Task/reads/policy material can be rechecked
        against that identity without recapturing the changed working tree.
        Legacy freeze events have no such material; their usable review prompt
        can still provide a baseline when its complete current binding matches.
        """
        if not plan_row:
            return None
        try:
            plan_body = parse_json(plan_row["body"])
            if digest(plan_body) != plan_row["digest"]:
                return None
            event = self.latest_plan_freeze(task_row, plan_row)
            if event and {
                "snapshot_digest", "snapshot_format", "review_binding", "approved",
            } <= set(event):
                if (type(event["snapshot_digest"]) is not str or
                        type(event["snapshot_format"]) is not str or
                        type(event["review_binding"]) is not str or
                        event["approved"] != plan_row.get("approved")):
                    return None
                if "snapshot_manifest_blob" in event:
                    raw = self.rt.s.blob_get(event["snapshot_manifest_blob"])
                    need(len(raw) <= MAX_FROZEN_SNAPSHOT_BYTES, "too_large",
                         "Frozen plan snapshot manifest exceeds its bounded size")
                    snapshot = parse_json(raw, limit=MAX_FROZEN_SNAPSHOT_BYTES)
                    need(canonical(snapshot) == raw, "integrity_error",
                         "Frozen plan snapshot manifest is not canonical")
                    if snapshot == EMPTY_SNAPSHOT:
                        pass
                    else:
                        from .verification_materials import validate_sealed_snapshot
                        validate_sealed_snapshot(self.rt.s, snapshot)
                    need(snapshot.get("digest") == event["snapshot_digest"] and
                         snapshot.get("format") == event["snapshot_format"],
                         "integrity_error", "Frozen plan snapshot identity differs from its manifest")
                else:
                    # Older freeze events retained only the digest and format.
                    # They remain usable for checking their historical PASS,
                    # but cannot materialize that baseline for a new review.
                    snapshot = {"format": event["snapshot_format"], "repos": {},
                                "digest": event["snapshot_digest"]}
                current = self.test_plan(actor, task_row, plan_body,
                                         store_snapshot_blobs=False,
                                         snapshot_override=snapshot)
                if current["binding"] != event["review_binding"]:
                    return None
                if require_manifest and not self._valid_full_snapshot(snapshot):
                    return None
                return snapshot

            # Pre-binding events contain only the plan digest. Recover the
            # baseline from the newest valid test-plan judgment whose prompt
            # still matches today's Task revision, reads, dependencies,
            # policy, and exact frozen plan. Invalid executions and other
            # roles never define this baseline.
            from .observed_receipts import ordered_observed_receipts
            receipts = ordered_observed_receipts(
                self.rt, project=task_row["project"], subject=task_row["id"],
                role="test_plan", binding=None,
            )
            for observed in reversed(receipts):
                receipt = observed["body"]
                if not self.rt.g._usable_review_judgment(receipt):
                    continue
                try:
                    prompt = parse_json(self.rt.s.blob_get(receipt["input_blob"]))
                    if not isinstance(prompt, dict):
                        continue
                    context = prompt.get("context") if isinstance(prompt, dict) else None
                    baseline = context.get("review_snapshot") if isinstance(context, dict) else None
                    if (prompt.get("role") != "test_plan" or
                            prompt.get("subject") != task_row["id"] or
                            prompt.get("binding") != receipt.get("binding") or
                            not isinstance(context, dict) or context.get("test_plan") != plan_body or
                            not isinstance(baseline, dict) or
                            baseline.get("digest") != receipt.get("snapshot") or
                            type(baseline.get("format")) is not str):
                        continue
                    snapshot = {"format": baseline["format"], "repos": {},
                                "digest": baseline["digest"]}
                    product = receipt.get("work_product")
                    if isinstance(product, dict) and isinstance(product.get("snapshot_blob"), str):
                        raw_snapshot = self.rt.s.blob_get(product["snapshot_blob"])
                        if len(raw_snapshot) > MAX_FROZEN_SNAPSHOT_BYTES:
                            continue
                        full_snapshot = parse_json(raw_snapshot, limit=MAX_FROZEN_SNAPSHOT_BYTES)
                        need(canonical(full_snapshot) == raw_snapshot,
                             "integrity_error", "Retained plan snapshot is not canonical")
                        if full_snapshot == EMPTY_SNAPSHOT:
                            pass
                        else:
                            from .verification_materials import validate_sealed_snapshot
                            validate_sealed_snapshot(self.rt.s, full_snapshot)
                        if (full_snapshot.get("digest") == baseline["digest"] and
                                full_snapshot.get("format") == baseline["format"]):
                            snapshot = full_snapshot
                    if require_manifest and not self._valid_full_snapshot(snapshot):
                        continue
                    current = self.test_plan(actor, task_row, plan_body,
                                             store_snapshot_blobs=False,
                                             snapshot_override=snapshot)
                    if current["binding"] == receipt.get("binding"):
                        return snapshot
                except (Fault, KeyError, TypeError, ValueError):
                    continue
            return None
        except (Fault, KeyError, TypeError, ValueError):
            return None
