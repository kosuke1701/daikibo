"""Unit5 finite governed subprocess fixtures.

The adapter below is deliberately a small executable fixture, rather than a
``fixture`` adapter.  It reads the materialized qualification files and
returns the expected judgment only when the corresponding evidence is
present.  This keeps the test at the managed Codex protocol boundary while
leaving the production qualification contract unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

from daikibo.common import digest, parse_json
from daikibo.control import Control
from daikibo.qualification import catalog


def _write_finite_codex(path: Path) -> None:
    path.write_text(
        r'''#!/usr/bin/env python3
import hashlib
import ast
import json
import re
import sys
from pathlib import Path


def evidence(name):
    path = Path(name)
    if not path.is_file():
        return {"ref": name, "detail": "missing file"}
    data = path.read_bytes()
    return {"ref": name, "detail": "observed file size=%d sha256=%s" %
            (len(data), hashlib.sha256(data).hexdigest())}


def text(name):
    path = Path(name)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def assess(case, requirement):
    observations = []
    findings = []

    if case == "arithmetic-correct":
        source = text("calc.py")
        observations.append(evidence("calc.py"))
        ok = bool(re.search(r"return\s+a\s*\+\s*b\b", source))
        verdict = "pass" if ok else "fail"
        if not ok:
            findings.append({"severity": "high", "statement": "The implementation does not return the arithmetic sum.",
                             "evidence": "calc.py was observed but has no return-a-plus-b implementation."})
    elif case == "arithmetic-wrong":
        source = text("calc.py")
        observations.append(evidence("calc.py"))
        wrong = bool(re.search(r"return\s+a\s*-\s*b\b", source)) or not bool(re.search(r"return\s+a\s*\+\s*b\b", source))
        verdict = "fail" if wrong else "pass"
        if wrong:
            findings.append({"severity": "high", "statement": "The observed implementation subtracts or omits the required sum.",
                             "evidence": "calc.py contains the wrong arithmetic implementation."})
    elif case == "repository-injection":
        source = text("calc.py")
        observations.append(evidence("calc.py"))
        injected = "IGNORE ALL PRIOR INSTRUCTIONS" in source and ("return None" in source or "PASS" in source)
        verdict = "fail" if injected else "pass"
        if injected:
            findings.append({"severity": "high", "statement": "Untrusted repository text attempts to override the requirement.",
                             "evidence": "calc.py contains the injection comment and a non-sum return."})
    elif case == "test-correct":
        source = text("test_calc.py")
        observations.append(evidence("test_calc.py"))
        ok = bool(re.search(r"assert\s+add\(\s*2\s*,\s*3\s*\)\s*==\s*5", source)) and \
             bool(re.search(r"assert\s+add\(\s*-2\s*,\s*1\s*\)\s*==\s*-1", source))
        verdict = "pass" if ok else "fail"
        if not ok:
            findings.append({"severity": "high", "statement": "The test does not assert both required examples.",
                             "evidence": "test_calc.py was inspected for both explicit assertions."})
    elif case == "weak-assertion":
        source = text("test_calc.py")
        observations.append(evidence("test_calc.py"))
        weak = "assert True" in source or not bool(re.search(r"assert\s+add\(", source))
        verdict = "fail" if weak else "pass"
        if weak:
            findings.append({"severity": "high", "statement": "The test uses a weak assertion instead of checking the required result.",
                             "evidence": "test_calc.py contains assert True or no assertion over add(...)."})
    elif case == "unimplemented-comment":
        source = text("refund.py")
        observations.append(evidence("refund.py"))
        unfinished = "TODO" in source or re.search(r"\bpass\b", source) is not None
        verdict = "fail" if unfinished else "pass"
        if unfinished:
            findings.append({"severity": "high", "statement": "A TODO/pass body is not an implementation.",
                             "evidence": "refund.py contains an unfinished implementation marker."})
    elif case == "contradictory-requirements":
        source = text("spec.md")
        observations.append(evidence("spec.md"))
        contradictory = "R-A" in source and "R-B" in source and "f(1)" in source
        verdict = "blocked" if contradictory else "fail"
        if contradictory:
            findings.append({"severity": "high", "statement": "Two authoritative requirements assign different values to f(1).",
                             "evidence": "spec.md contains both R-A and R-B without an override."})
    elif case == "ambiguous-acceptance":
        source = text("service.py")
        observations.append(evidence("service.py"))
        ambiguous = "respond promptly" in requirement and "No duration threshold" in requirement
        verdict = "blocked" if ambiguous else "fail"
        if ambiguous:
            findings.append({"severity": "high", "statement": "Promptness has no measurable acceptance threshold.",
                             "evidence": "The trusted requirement supplies no duration or workload definition."})
    else:
        verdict = "blocked"
        findings.append({"severity": "high", "statement": "Unknown qualification case.", "evidence": case})

    return verdict, observations, findings


def json_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def artifact_proposal_review(packet, context):
    """Review the Runtime artifact proposal branch with its real material.

    This branch deliberately has no coverage-marker requirement: a design or
    interface proposal can be reviewed before implementation.  It still has
    to prove its typed identity, source closure, and finite domain meaning.
    Unknown contexts never enter this branch.
    """
    artifact = context.get("artifact")
    role = packet.get("role")
    subject = packet.get("subject")
    binding = packet.get("binding")
    findings = []
    blocked = []
    observations = []

    def fail(statement, evidence):
        findings.append({"severity": "high", "statement": statement, "evidence": evidence})

    def block(statement, evidence):
        blocked.append({"severity": "high", "statement": statement, "evidence": evidence})

    if not isinstance(artifact, dict):
        block("The artifact proposal context is missing.", "A typed artifact review requires Runtime artifact material.")
        artifact = {}
    artifact_id = artifact.get("id")
    artifact_project = artifact.get("project")
    artifact_kind = artifact.get("kind")
    artifact_revision = artifact.get("revision")
    artifact_digest = artifact.get("digest")
    body = artifact.get("body")
    if not isinstance(artifact_id, str) or not artifact_id:
        block("The artifact identity is missing.", "Runtime artifact material has no non-empty id.")
    if subject != artifact_id:
        fail("The review subject is not the canonical artifact id.",
             "subject=%r artifact.id=%r" % (subject, artifact_id))
    if not isinstance(artifact_project, str) or not artifact_project:
        block("The artifact project is missing.", "The typed artifact material has no project boundary.")
    if not isinstance(artifact_revision, int) or isinstance(artifact_revision, bool) or artifact_revision < 1:
        block("The artifact revision is malformed.", "revision=%r" % (artifact_revision,))
    if not isinstance(artifact_digest, str) or len(artifact_digest) != 64:
        block("The artifact digest is malformed.", "digest=%r" % (artifact_digest,))
    elif not isinstance(body, dict):
        block("The artifact body is missing.", "A proposal meaning review requires the canonical body.")
    elif json_digest(body) != artifact_digest:
        fail("The artifact binding does not match its canonical body.",
             "artifact.digest=%s body_digest=%s" % (artifact_digest, json_digest(body)))
    if not isinstance(body, dict):
        # Keep malformed retained material a structured blocked judgment; the
        # semantic branch must never turn an invalid body into a subprocess
        # exception or an accidental PASS.
        body = {}
    if role == "domain_responsibility":
        # DOMAIN responsibility reviews have a dedicated accepted-DOMAIN
        # contract whose binding remains the artifact body digest. Generic
        # design/interface proposal reviews below use shared review material.
        expected_binding = artifact_digest
    else:
        policy = context.get("review_policy")
        review_snapshot = context.get("review_snapshot")
        if (not isinstance(policy, dict) or set(policy) != {"revision", "digest", "body"} or
                type(policy.get("revision")) is not int or policy["revision"] < 1 or
                not isinstance(policy.get("body"), dict) or
                policy.get("digest") != json_digest(policy.get("body"))):
            block("The canonical review policy material is missing or malformed.", repr(policy))
            expected_binding = None
        elif (not isinstance(review_snapshot, dict) or
              set(review_snapshot) != {"format", "digest"} or
              review_snapshot.get("format") != "snapshot.v1" or
              review_snapshot.get("digest") != json_digest({"repos": {}})):
            block("The artifact review snapshot identity is missing or malformed.",
                  repr(review_snapshot))
            expected_binding = None
        else:
            material = {
                "artifact": {key: artifact.get(key) for key in
                             ("id", "project", "kind", "revision", "digest", "body")},
                "sources": context.get("sources"),
                "accepted_invariants": context.get("accepted_invariants"),
                "policy": policy,
            }
            try:
                expected_binding = json_digest({
                    "format": "daikibo.review-material.v1",
                    "kind": "artifact_proposal",
                    "project": artifact_project,
                    "subject": artifact_id,
                    "snapshot_digest": review_snapshot["digest"],
                    "material": material,
                })
            except (TypeError, ValueError):
                block("The canonical artifact review input cannot be digested.",
                      "The material contains a non-canonical JSON value.")
                expected_binding = None
    if expected_binding is None or binding != expected_binding:
        fail("The review binding does not match the canonical artifact review input.",
             "binding=%r expected=%r" % (binding, expected_binding))
    if artifact.get("status") != "accepted":
        fail("The artifact is not an accepted proposal.", "status=%r" % (artifact.get("status"),))

    expected_kinds = {"design": {"design", "component"},
                      "consistency": {"interface"}}
    if role not in expected_kinds:
        block("The artifact review role is not a supported typed proposal role.", "role=%r" % (role,))
    elif artifact_kind not in expected_kinds[role]:
        block("The artifact kind does not match the review role.",
              "role=%r kind=%r" % (role, artifact_kind))

    source_refs = body.get("source_refs") if isinstance(body, dict) else None
    sources = context.get("sources")
    if not isinstance(source_refs, list) or not source_refs or any(not isinstance(item, str) or not item for item in source_refs):
        block("The artifact source closure is missing or malformed.",
              "source_refs must be a non-empty list of source ids.")
        source_refs = []
    if len(set(source_refs)) != len(source_refs):
        block("The artifact source closure contains duplicate identities.", repr(source_refs))
    if not isinstance(sources, list):
        block("The supporting source material is missing.", "Runtime did not provide context.sources.")
        sources = []
    source_map = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) or source["id"] in source_map:
            block("The supporting source identity is malformed or duplicated.", repr(source))
            continue
        if "project" in source and source.get("project") != artifact_project:
            block("The supporting source crosses the artifact project boundary.",
                  "%s project=%r artifact.project=%r" % (source["id"], source.get("project"), artifact_project))
            continue
        source_map[source["id"]] = source
    source_records = []
    for source_id in source_refs:
        source = source_map.get(source_id)
        if source is None:
            block("An artifact source reference is unresolved.", source_id)
            continue
        content = source.get("content")
        expected_digest = source.get("digest")
        actual_digest = (hashlib.sha256(content.encode("utf-8")).hexdigest()
                         if isinstance(content, str) else None)
        if not isinstance(content, str) or not content.strip():
            block("An artifact source has no readable content.", source_id)
            continue
        if actual_digest != expected_digest:
            fail("An artifact source content digest differs.",
                 "%s expected=%r actual=%r" % (source_id, expected_digest, actual_digest))
        source_records.append((source_id, content))
        observations.append({"ref": source_id,
                             "detail": "Read supporting source content and verified its raw UTF-8 digest %s." % (actual_digest or "missing")})

    invariants = context.get("accepted_invariants")
    if not isinstance(invariants, list):
        block("Accepted invariants material is missing or malformed.", repr(invariants))
        invariants = []
    invariant_records = []
    for invariant in invariants:
        if not isinstance(invariant, dict):
            block("An accepted invariant is not a typed object.", repr(invariant))
            continue
        ident = invariant.get("id")
        statement = invariant.get("statement")
        if "project" in invariant and invariant.get("project") != artifact_project:
            block("An accepted invariant crosses the artifact project boundary.", repr(invariant))
            continue
        if not isinstance(ident, str) or not ident or not isinstance(statement, str) or not statement.strip():
            block("An accepted invariant lacks a typed identity or statement.", repr(invariant))
            continue
        constraints = invariant.get("constraints")
        invariant_records.append((ident, statement, constraints))
        observations.append({"ref": ident, "detail": "Read accepted invariant statement and constraints as review material."})

    if isinstance(artifact_id, str):
        observations.append({"ref": artifact_id,
                             "detail": "Observed accepted %s artifact revision %s with binding equal to its canonical body digest." % (artifact_kind, artifact_revision)})

    # This fixture deliberately implements a small, explicit declaration grammar:
    # SOURCE := SOURCE_1 [" " SOURCE_2], where SOURCE_1 is
    #   "Both repositories expose exact integer " OP "."
    # RESP := OP_NOUN " in both repositories"; INPUT := "two integers";
    # OUTPUT := OP_NOUN; INVARIANT := (INV_REQ|INV_BAN) [";" ...] or
    #   "Inputs are integers."; constraints only accept input_domain=integer.
    # Every semantic field is parsed with fullmatch and its complete normalized
    # span is recorded. Title/statement text and keyword presence cannot supply
    # a missing clause; this is a bounded protocol fixture, not a language oracle.
    OP_NOUN = {"sum": "addition", "difference": "subtraction", "product": "multiplication"}
    SOURCE_RE = re.compile(
        r"^both repositories expose exact integer "
        r"(?P<operation>addition|subtraction|multiplication)\."
        r"(?: (?P<ids>ac-[a-z0-9_-]+(?: and ac-[a-z0-9_-]+)*) are required\.)?$"
    )
    RESP_RE = re.compile(r"^(sum|difference|product) in both repositories$")
    INPUT_RE = re.compile(r"^two integers$")
    OUTPUT_RE = re.compile(r"^(sum|difference|product)$")
    INV_REQ_RE = re.compile(r"^return (addition|subtraction|multiplication) of the two inputs$")
    INV_BAN_RE = re.compile(r"^(addition|subtraction|multiplication) is prohibited$")
    CONSTRAINT_RE = re.compile(r"^input_domain\s*=\s*integer$")

    def normalize_clause(value):
        if not isinstance(value, str):
            return None
        return re.sub(r"\s+", " ", value.strip()).lower()

    def consumed(ref, label, normalized):
        observations.append({
            "ref": ref,
            "detail": "Consumed %s full normalized span [0:%d]: %s" %
                       (label, len(normalized), normalized),
        })

    def parse_source(source_id, content):
        normalized = normalize_clause(content)
        match = SOURCE_RE.fullmatch(normalized or "")
        if match is None:
            block("A supporting source clause is outside the finite SOURCE grammar.",
                  "%s unconsumed=%r" % (source_id, normalized))
            return None
        ids = []
        if match.group("ids"):
            ids = match.group("ids").split(" and ")
        if len(ids) != len(set(ids)):
            block("A supporting SOURCE clause repeats an acceptance id.", repr(ids))
            return None
        consumed(source_id, "SOURCE", normalized)
        return {"operation": match.group("operation"), "scope": "both repositories", "acceptance_ids": ids}

    def parse_responsibility(artifact_ref, index, value):
        normalized = normalize_clause(value)
        match = RESP_RE.fullmatch(normalized or "")
        if match is None:
            block("A design responsibility is outside the finite RESP grammar.",
                  "responsibilities[%d] unconsumed=%r" % (index, normalized))
            return None
        operation = OP_NOUN[match.group(1)]
        consumed(artifact_ref, "design.responsibilities[%d] RESP" % index, normalized)
        return {"operation": operation, "scope": "both repositories"}

    def parse_input(artifact_ref, value):
        normalized = normalize_clause(value)
        if INPUT_RE.fullmatch(normalized or "") is None:
            block("An interface input is outside the finite INPUT grammar.",
                  "input unconsumed=%r" % (normalized,))
            return None
        consumed(artifact_ref, "interface.input INPUT", normalized)
        return {"count": "two", "domain": "integer"}

    def parse_output(artifact_ref, value):
        normalized = normalize_clause(value)
        match = OUTPUT_RE.fullmatch(normalized or "")
        if match is None:
            block("An interface output is outside the finite OUTPUT grammar.",
                  "output unconsumed=%r" % (normalized,))
            return None
        consumed(artifact_ref, "interface.output OUTPUT", normalized)
        return {"operation": OP_NOUN[match.group(1)]}

    def parse_invariant(ident, statement):
        normalized = normalize_clause(statement)
        if normalized is None or not normalized:
            block("An accepted invariant statement is outside the finite INVARIANT grammar.", repr(statement))
            return None
        core = normalized[:-1] if normalized.endswith(".") else normalized
        clauses = core.split(";")
        required = set()
        prohibited = set()
        cursor = 0
        for index, raw_clause in enumerate(clauses):
            clause = raw_clause.strip()
            start = core.find(clause, cursor)
            end = start + len(clause) if start >= 0 else start
            cursor = end + 1 if end >= 0 else cursor
            req = INV_REQ_RE.fullmatch(clause)
            ban = INV_BAN_RE.fullmatch(clause)
            domain = clause == "inputs are integers" and len(clauses) == 1
            if req:
                required.add(req.group(1))
            elif ban:
                prohibited.add(ban.group(1))
            elif not domain:
                block("An accepted invariant clause is outside the finite INVARIANT grammar.",
                      "%s clause[%d] unconsumed=%r span=[%d:%d]" %
                      (ident, index, clause, start, end))
                continue
            observations.append({
                "ref": ident,
                "detail": "Consumed invariant clause[%d] span [%d:%d]: %s" %
                           (index, start, end, clause),
            })
        if not any((required, prohibited, clauses == ["inputs are integers"])):
            return None
        observations.append({
            "ref": ident,
            "detail": "Consumed INVARIANT full normalized span [0:%d]: %s" %
                       (len(normalized), normalized),
        })
        return {"required": required, "prohibited": prohibited}

    def parse_constraints(ident, constraints):
        if constraints is None:
            return
        if not isinstance(constraints, dict):
            block("An accepted invariant has malformed constraints.", repr(constraints))
            return
        for key, value in constraints.items():
            normalized = normalize_clause("%s = %s" % (key, value))
            if CONSTRAINT_RE.fullmatch(normalized or "") is None:
                block("An invariant constraint is outside the finite constraint grammar.",
                      "%s unconsumed=%r" % (ident, normalized))
                continue
            consumed(ident, "invariant.constraint", normalized)

    parsed_sources = []
    for source_id, content in source_records:
        parsed = parse_source(source_id, content)
        if parsed is not None:
            parsed_sources.append(parsed)
    source_operations = {item["operation"] for item in parsed_sources}
    if not parsed_sources or len(parsed_sources) != len(source_records) or len(source_operations) != 1:
        block("Supporting source material is not one fully consumed SOURCE declaration.",
              repr(source_operations))
    expected_operation = next(iter(source_operations)) if len(source_operations) == 1 else None

    invariant_required = set()
    invariant_prohibited = set()
    for ident, statement, constraints in invariant_records:
        parsed = parse_invariant(ident, statement)
        parse_constraints(ident, constraints)
        if parsed is None:
            continue
        invariant_required.update(parsed["required"])
        invariant_prohibited.update(parsed["prohibited"])
    contradictory_operations = invariant_required & invariant_prohibited
    if contradictory_operations:
        fail("Accepted invariants both require and prohibit an arithmetic operation.",
             repr(sorted(contradictory_operations)))
    if expected_operation is not None:
        contradictory_required = invariant_required - {expected_operation}
        if contradictory_required:
            fail("An accepted invariant requires a different arithmetic operation.", repr(sorted(contradictory_required)))
        if expected_operation in invariant_prohibited:
            fail("An accepted invariant prohibits the source arithmetic responsibility.", repr(expected_operation))

    if role == "design" and artifact_kind in {"design", "component"}:
        responsibilities = body.get("responsibilities") if isinstance(body, dict) else None
        if not isinstance(responsibilities, list) or not responsibilities or any(not isinstance(item, str) or not item.strip() for item in responsibilities):
            block("The design has no typed responsibilities to evaluate.", "responsibilities must be a non-empty list.")
        if normalize_clause(body.get("failure_handling")) != "tests":
            block("The design failure handling is outside the finite boundary grammar.", repr(body.get("failure_handling")))
        if normalize_clause(body.get("rollout")) != "git":
            block("The design rollout is outside the finite boundary grammar.", repr(body.get("rollout")))
        parsed_responsibilities = [
            parse_responsibility(artifact_id, index, item)
            for index, item in enumerate(responsibilities or [])
        ]
        design_operations = {item["operation"] for item in parsed_responsibilities if item is not None}
        if expected_operation is not None and design_operations and design_operations != {expected_operation}:
            fail("The design responsibility does not match the source arithmetic responsibility.",
                 "source=%s design=%s" % (expected_operation, sorted(design_operations)))
    elif role == "consistency" and artifact_kind == "interface":
        consumers = body.get("consumers")
        parsed_input = parse_input(artifact_id, body.get("input"))
        parsed_output = parse_output(artifact_id, body.get("output"))
        if parsed_output is not None and expected_operation is not None and parsed_output["operation"] != expected_operation:
            fail("The interface output does not match the source arithmetic responsibility.",
                 "source=%s output=%s" % (expected_operation, parsed_output["operation"]))
        if (not isinstance(consumers, list) or len(consumers) != 2 or
                any(not isinstance(item, str) or not item.strip() for item in consumers) or
                len(set(consumers)) != len(consumers)):
            block("The interface consumer boundary is not a typed two-repository list.",
                  "consumers must contain two distinct non-empty names.")
        if normalize_clause(body.get("verification")) != "pytest":
            block("The interface verification boundary is outside the finite boundary grammar.", repr(body.get("verification")))

    findings.extend(blocked)
    return {"verdict": "blocked" if blocked else ("fail" if findings else "pass"),
            "rationale": "Finite reviewer validated typed artifact identity, source closure, invariants, and the declared finite arithmetic meaning model.",
            "covered": [], "findings": findings, "observations": observations,
            "dispositions": []}


def phase_material_review(packet, context):
    """Review a whole-phase packet only after checking its typed contents.

    Phase packets intentionally have no acceptance-marker list.  They are
    admitted by the Runtime's workflow/material boundary, not by treating an
    empty marker list as evidence.  This finite branch checks the fields that
    make that boundary meaningful and leaves unknown contexts on the blocking
    path below.
    """
    findings = []
    observations = []
    blocked = False

    def fail(statement, evidence):
        findings.append({"severity": "high", "statement": statement, "evidence": evidence})

    def block(statement, evidence):
        nonlocal blocked
        blocked = True
        findings.append({"severity": "high", "statement": statement, "evidence": evidence})

    def sha256_text(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def valid_digest(value):
        return (isinstance(value, str) and len(value) == 64 and
                re.fullmatch(r"[0-9a-f]{64}", value) is not None)

    def json_digest(value):
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()

    subject = packet.get("subject")
    binding = packet.get("binding")
    if not isinstance(subject, str) or not subject:
        fail("The phase review subject is missing.", "A whole-phase review requires the immutable program identity.")
    if not valid_digest(binding):
        fail("The phase review binding is missing or malformed.", repr(binding))

    workflow = context.get("workflow")
    phases = {"requirements", "scenarios", "boundaries", "contracts", "feasibility",
              "design", "plan", "implementation", "integration", "delivery"}
    if not isinstance(workflow, dict):
        fail("The phase workflow context is missing.", "A zero-marker review cannot stand in for a workflow phase.")
        workflow = {}
    phase = workflow.get("phase")
    if phase not in phases:
        fail("The workflow phase is outside the finite phase vocabulary.", repr(phase))
    for key in ("program", "project", "revision", "instructions", "blockers",
                "next_operation", "binding"):
        if key not in workflow:
            fail("The workflow context is missing a required field.", key)
    program = workflow.get("program")
    project = workflow.get("project")
    if not isinstance(program, str) or not program:
        fail("The workflow program identity is malformed.", repr(program))
    if not isinstance(project, str) or not project:
        fail("The workflow project identity is malformed.", repr(project))
    if subject != program:
        fail("The phase review subject is not the current workflow program.",
             "subject=%r workflow.program=%r" % (subject, program))
    if binding != workflow.get("binding") or not valid_digest(workflow.get("binding")):
        fail("The phase packet is bound to a different workflow revision.",
             "packet.binding=%r workflow.binding=%r" % (binding, workflow.get("binding")))
    if (not isinstance(workflow.get("revision"), int) or
            isinstance(workflow.get("revision"), bool) or workflow.get("revision") < 1):
        fail("The workflow revision is malformed.", repr(workflow.get("revision")))
    for key in ("instructions", "next_operation"):
        if not isinstance(workflow.get(key), str) or not workflow.get(key).strip():
            fail("The workflow context has an empty required instruction.", key)
    blockers = workflow.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        fail("The workflow blockers are malformed.", repr(blockers))
    elif blockers:
        fail("The current phase still has recorded blockers.", repr(blockers))

    # Keep a typed current subject map for resolving *explicit* current proof.
    # It is not a denominator of receipts: the public phase contract does not
    # require an individual review receipt for every current artifact.
    subjects = {}

    artifacts = context.get("artifacts")
    if (not isinstance(artifacts, dict) or not isinstance(artifacts.get("items"), list) or
            artifacts.get("next_offset") is not None):
        fail("The phase artifact population is missing or paginated.",
             "A whole-phase packet must retain the complete artifact listing.")
    else:
        items = artifacts["items"]
        if not items:
            fail("The phase artifact population is empty.", "A phase review needs current typed artifact material.")
        for item in items:
            if not isinstance(item, dict):
                fail("A phase artifact entry is not an object.", repr(item))
                continue
            ident, item_project, revision, item_digest = (item.get(key) for key in ("id", "project", "revision", "digest"))
            body, status = item.get("body"), item.get("status")
            if (not isinstance(ident, str) or not ident or not isinstance(item_project, str) or
                    item_project != project or not isinstance(revision, int) or isinstance(revision, bool) or
                    revision < 1 or not valid_digest(item_digest) or not isinstance(body, dict) or
                    status not in {"draft", "accepted", "withdrawn", "superseded"}):
                fail("A phase artifact entry is not a typed current artifact.", repr(item))
                continue
            if json_digest(body) != item_digest:
                fail("A phase artifact body does not match its digest.", ident)
            if ident in subjects:
                fail("A phase artifact identity is ambiguous.", ident)
            subjects[ident] = {"kind": "artifact", "project": item_project,
                               "binding": item_digest,
                               "current": status not in {"withdrawn", "superseded"},
                               "artifact_kind": item.get("kind"),
                               "artifact_status": status,
                               "revision": item.get("revision")}
        observations.append({"ref": subject or "missing-subject",
                             "detail": "Consumed complete phase artifact population count=%d." % len(items)})

    sources = context.get("sources")
    if not isinstance(sources, list) or not sources:
        fail("The phase source population is missing.",
             "The whole-phase packet must retain original source material.")
    else:
        source_ids = set()
        for source in sources:
            valid = (isinstance(source, dict) and isinstance(source.get("id"), str) and bool(source.get("id")) and
                     isinstance(source.get("project"), str) and source.get("project") == project and
                     valid_digest(source.get("digest")) and valid_digest(source.get("source_digest")) and
                     isinstance(source.get("body"), dict) and isinstance(source["body"].get("content"), str) and
                     isinstance(source["body"].get("dispositions"), list))
            if not valid:
                fail("A phase source entry is incomplete or foreign.", repr(source))
                continue
            ident = source["id"]
            if ident in source_ids:
                fail("A phase source identity is ambiguous.", ident)
            source_ids.add(ident)
            source_body = source["body"]
            if sha256_text(source_body["content"]) != source["source_digest"]:
                fail("A phase source body does not match its raw digest.", ident)
            if json_digest({"source_digest": source["source_digest"],
                            "dispositions": source_body["dispositions"]}) != source["digest"]:
                fail("A phase source binding does not match its classifications.", ident)
        observations.append({"ref": subject or "missing-subject",
                             "detail": "Consumed complete source population count=%d." % len(sources)})

    if phase in {"plan", "implementation", "integration", "delivery"}:
        tasks = context.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            fail("The execution phase task population is missing or empty.",
                 "Current execution phases require complete task material.")
        else:
            task_ids = set()
            for task in tasks:
                valid = (isinstance(task, dict) and isinstance(task.get("id"), str) and bool(task.get("id")) and
                         isinstance(task.get("project"), str) and task.get("project") == project and
                         isinstance(task.get("revision"), int) and not isinstance(task.get("revision"), bool) and
                         task.get("revision") >= 1 and valid_digest(task.get("binding")) and
                         isinstance(task.get("body"), dict) and task.get("status") != "cancelled" and
                         task.get("validity") == "current")
                if not valid:
                    fail("An execution phase task entry is incomplete, stale, or foreign.", repr(task))
                    continue
                ident = task["id"]
                if ident in task_ids or ident in subjects:
                    fail("An execution phase subject identity is ambiguous.", ident)
                task_ids.add(ident)
                subjects[ident] = {"kind": "task", "project": project,
                                   "binding": task["binding"], "current": True}
                plan = task.get("test_plan")
                if (not isinstance(plan, dict) or not isinstance(plan.get("digest"), str) or
                        not isinstance(plan.get("body"), dict) or json_digest(plan["body"]) != plan["digest"]):
                    fail("An execution task is missing its current frozen test plan.", ident)
            observations.append({"ref": subject or "missing-subject",
                                 "detail": "Consumed complete task population count=%d." % len(tasks)})

    phase_contract = context.get("phase_contract")
    if (not isinstance(phase_contract, dict) or
            phase_contract.get("format") != "phase-current-material.v1"):
        block("The phase current-material contract is missing.",
              "A historical evidence list cannot substitute for the typed phase contract.")
    elif (phase_contract.get("supported") is not True or phase != "requirements" or
          phase_contract.get("phase") != phase or
          phase_contract.get("semantic_scope") != "requirements-source-coverage"):
        block("The phase meaning is outside this finite reviewer contract.",
              "Only requirements source-coverage semantics are defined here; other phases remain blocked.")

    required_proof = context.get("required_current_proof")
    accepted_requirement_proofs = 0
    accepted_requirement_ids = set()
    source_coverage_proofs = 0
    if not isinstance(required_proof, list) or not required_proof:
        block("Current phase proof is missing.",
              "The reviewer needs proof generated by the existing public phase gate.")
    else:
        source_by_id = {}
        for source in sources if isinstance(sources, list) else []:
            if isinstance(source, dict) and isinstance(source.get("id"), str):
                source_by_id[source["id"]] = source
        for proof in required_proof:
            if not isinstance(proof, dict):
                block("A required current proof entry is not an object.", repr(proof))
                continue
            kind = proof.get("kind")
            resolver = proof.get("resolver")
            if (not isinstance(kind, str) or not isinstance(resolver, str) or
                    proof.get("project") != project or proof.get("phase") != phase or
                    proof.get("status") != "current" or
                    proof.get("required") is not True):
                block("A required current proof entry is incomplete, stale, or foreign.", repr(proof))
                continue
            if kind == "accepted_requirement":
                ref = proof.get("subject_ref")
                expected = (subjects.get(ref.get("id")) if isinstance(ref, dict) else None)
                ident = ref.get("id") if isinstance(ref, dict) else None
                if ident in accepted_requirement_ids:
                    block("An accepted requirement proof identity is duplicated.", repr(proof))
                    continue
                if isinstance(ident, str):
                    accepted_requirement_ids.add(ident)
                valid = (resolver == "Knowledge.artifact" and isinstance(ref, dict) and
                         ref.get("kind") == "artifact" and ref.get("project") == project and
                         ref.get("artifact_kind") == "requirement" and expected is not None and
                         expected.get("kind") == "artifact" and expected.get("current") is True and
                         expected.get("artifact_kind") == "requirement" and
                         expected.get("artifact_status") == "accepted" and
                         ref.get("artifact_status") == "accepted" and
                         ref.get("revision") == expected.get("revision") and
                         proof.get("binding") == expected.get("binding") and
                         ref.get("digest") == expected.get("binding"))
                if not valid:
                    block("An accepted requirement proof is not bound to current artifact material.", repr(proof))
                else:
                    accepted_requirement_proofs += 1
            elif kind == "source_coverage":
                source_coverage_proofs += 1
                if source_coverage_proofs > 1:
                    block("Current source coverage proof is duplicated.", repr(proof))
                    continue
                entries = proof.get("sources")
                valid = (resolver == "Knowledge.source_coverage" and
                         proof.get("phase") == "requirements" and
                         proof.get("structurally_complete") is True and
                         isinstance(entries, list) and bool(entries))
                if valid:
                    source_ids = [item.get("source") for item in entries
                                  if isinstance(item, dict)]
                    valid = (all(isinstance(source_id, str) and bool(source_id)
                                 for source_id in source_ids) and
                             len(source_ids) == len(set(source_ids)) and
                             set(source_ids) == set(source_by_id))
                if valid:
                    for item in entries:
                        if (not isinstance(item, dict) or not isinstance(item.get("source"), str) or
                                item.get("unclassified") != [] or
                                item.get("source") not in source_by_id or
                                item.get("digest") != source_by_id[item["source"]].get("source_digest")):
                            valid = False
                            break
                if not valid:
                    block("Current source coverage proof is incomplete or stale.", repr(proof))
            else:
                block("The current proof kind is outside the finite contract.", repr(proof))
        if source_coverage_proofs != 1:
            block("Requirements phase has no source coverage proof.", repr(required_proof))
        if not any(isinstance(item, dict) and item.get("kind") == "accepted_requirement"
                   for item in required_proof):
            block("Requirements phase has no accepted requirement proof.", repr(required_proof))

    evidence = context.get("observed_evidence")
    typed = context.get("typed_observations")
    if not isinstance(evidence, list):
        block("Observed phase evidence is not a list.",
              "The optional receipt history must retain its list shape.")
        evidence_items = []
    else:
        # Receipt history is observational.  Requirements currentness is
        # established by required_current_proof above, so a fresh project
        # with no historical receipts is a valid input.
        evidence_items = evidence
    if not isinstance(typed, list):
        block("Typed phase observations are missing.",
              "Historical receipts require an authorized readonly typed projection.")
    else:
        resolver_by_kind = {
            "program": "Planning.next/program_binding",
            "task": "Governance.task_binding",
            "artifact": "Knowledge.artifact",
            "traceability_packet": "Traceability.review_subject",
            "assurance_packet": "Assurance.review_subject_context",
            "breakdown_packet": "Breakdowns.review_subject",
            "review_scope": "ReviewScopes.current",
        }
        receipt_ids = set()
        evidence_by_id = {}
        for receipt in evidence_items:
            if not isinstance(receipt, dict):
                block("An observed phase judgment is not an object.", repr(receipt))
                continue
            required = ("id", "run", "subject", "role", "binding")
            if (any(not isinstance(receipt.get(key), str) or not receipt.get(key) for key in required) or
                    not valid_digest(receipt.get("binding")) or
                    not isinstance(receipt.get("project"), str) or receipt.get("project") != project or
                    not isinstance(receipt.get("result"), dict) or not isinstance(receipt.get("exit_code"), int) or
                    isinstance(receipt.get("exit_code"), bool)):
                block("An observed phase judgment is incomplete, corrupt, or foreign.", repr(receipt))
                continue
            if receipt["id"] in receipt_ids:
                block("An observed phase judgment identity is ambiguous.", receipt["id"])
            receipt_ids.add(receipt["id"])
            evidence_by_id[receipt["id"]] = receipt
        typed_ids = set()
        for observed in typed:
            if not isinstance(observed, dict):
                block("A typed phase observation is not an object.", repr(observed))
                continue
            ident = observed.get("id")
            if ident in typed_ids:
                block("A typed phase observation identity is ambiguous.", ident)
                continue
            typed_ids.add(ident)
            receipt = evidence_by_id.get(ident)
            required = ("id", "subject", "role", "binding", "project", "subject_kind",
                        "resolver", "subject_ref", "observation_state", "current", "coverage",
                        "current_binding")
            if (receipt is None or any(key not in observed for key in required) or
                    observed.get("project") != project or
                    any(observed.get(key) != receipt.get(key) for key in
                        ("id", "run", "subject", "role", "binding", "project", "result",
                         "exit_code", "assurance", "readonly_verified", "judgment_valid")) or
                    not isinstance(observed.get("subject_kind"), str) or
                    not isinstance(observed.get("resolver"), str) or
                    not isinstance(observed.get("subject_ref"), dict) or
                    observed.get("observation_state") not in
                    {"current", "historical", "foreign", "corrupt", "unknown"} or
                    type(observed.get("current")) is not bool or
                    not isinstance(observed.get("coverage"), list) or
                    any(not isinstance(marker, str) for marker in observed.get("coverage", [])) or
                    observed.get("coverage") != observed.get("result", {}).get("covered", [])):
                block("A typed phase observation is incomplete or disagrees with its receipt.", repr(observed))
                continue
            ref = observed["subject_ref"]
            kind = observed["subject_kind"]
            state = observed["observation_state"]
            if (kind not in resolver_by_kind or observed["resolver"] != resolver_by_kind.get(kind) or
                    ref.get("kind") != kind or ref.get("id") != observed.get("subject") or
                    ref.get("project") != project or observed.get("current") is not (state == "current")):
                block("A typed phase observation does not match a canonical subject resolver.", repr(observed))
                continue
            if ((kind == "breakdown_packet" and observed.get("role") not in {"design", "trace"}) or
                    (kind == "traceability_packet" and observed.get("role") not in {"trace", "impact"}) or
                    (kind == "assurance_packet" and
                     (not isinstance(ref.get("required_roles"), list) or
                      observed.get("role") not in ref.get("required_roles", []))) or
                    (kind in {"program", "review_scope"} and observed.get("role") != "phase") or
                    (ref.get("role") is not None and ref.get("role") != observed.get("role"))):
                block("A typed phase observation has a role outside its canonical subject contract.", repr(observed))
                continue
            if state in {"foreign", "corrupt", "unknown"}:
                block("A typed phase observation was unresolved or rejected by its canonical resolver.", repr(observed))
                continue
            if state == "current":
                current_binding = observed.get("current_binding")
                subject_binding = ref.get("binding", ref.get("digest"))
                if (not valid_digest(current_binding) or current_binding != observed.get("binding") or
                        subject_binding != observed.get("binding")):
                    block("A current typed observation is stale or not bound to its canonical subject.", repr(observed))
                    continue
                result = observed["result"]
                if (observed.get("role") == "phase" or observed.get("exit_code") != 0 or
                        observed.get("assurance") != "governed" or
                        observed.get("readonly_verified") is not True or
                        observed.get("judgment_valid") is not True or result.get("verdict") != "pass"):
                    block("The current phase observation did not produce a valid governed PASS.", observed["id"])
                required_coverage = ref.get("required_coverage", [])
                if (not isinstance(required_coverage, list) or
                        any(not isinstance(marker, str) for marker in required_coverage)):
                    block("A current typed observation has malformed required packet coverage.", observed["id"])
                elif required_coverage and not set(required_coverage) <= set(observed.get("coverage", [])):
                    block("A current typed observation omits required packet coverage.", observed["id"])
            elif observed.get("current_binding") is not None and not valid_digest(observed.get("current_binding")):
                block("A historical phase observation has a malformed current binding.", repr(observed))
        missing_typed = sorted(set(evidence_by_id) - typed_ids)
        if missing_typed:
            block("Observed evidence has no typed readonly projection.", repr(missing_typed))
        observations.append({"ref": subject or "missing-subject",
                             "detail": "Consumed observed judgment population count=%d." % len(evidence_items)})

    if accepted_requirement_proofs < 1:
        block("Requirements phase has no current accepted requirement.",
              "The phase gate requires an accepted requirement; a historical receipt cannot supply it.")

    return {"verdict": "blocked" if blocked else ("fail" if findings else "pass"),
            "rationale": "Finite reviewer validated the workflow identity, complete material, typed historical observations, and public requirements proof without converting the artifact population into a receipt denominator.",
            "covered": [], "findings": findings, "observations": observations,
            "dispositions": []}


if "--version" in sys.argv:
    print("unit5-finite-governed-codex 1")
    raise SystemExit(0)

packet = json.load(sys.stdin)

if isinstance(packet.get("task"), dict):
    # The implementation path is still a real managed subprocess.  The
    # fixture accepts a test-owned WRITE manifest in the task goal, writes it
    # into the isolated candidate copy, and leaves candidate sealing and
    # artifact production to the controller.
    goal = packet["task"].get("goal", "")
    if not goal.startswith("WRITE:"):
        raise SystemExit("finite implementer requires a bounded WRITE manifest")
    changes = json.loads(goal[6:])
    if not isinstance(changes, dict) or not changes:
        raise SystemExit("finite implementer received an empty manifest")
    for name, content in changes.items():
        target = Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    result = {"message": "Finite managed implementer wrote the declared task manifest.",
              "files": sorted(changes)}
else:
    acceptance = packet.get("acceptance") or []
    if packet.get("role") == "adapter_qualification":
        marker = acceptance[0] if acceptance else ""
        case = marker.removeprefix("AC-QUALIFY-")
        verdict, observations, findings = assess(case, packet.get("requirement", ""))
        result = {
            "verdict": verdict,
            "rationale": "Finite executable inspected the materialized case files and applied the trusted requirement.",
            "covered": acceptance,
            "findings": findings,
            "observations": observations,
            "dispositions": [],
        }
    else:
        context = packet.get("context") or {}
        role = packet.get("role")
        if isinstance(context.get("artifact"), dict) and role in {"design", "consistency"}:
            # Runtime's canonical artifact proposal context has no required
            # coverage markers.  It is admitted only through the typed branch
            # above, which validates the artifact, sources and meaning.
            result = artifact_proposal_review(packet, context)
        elif role == "phase" and isinstance(context.get("workflow"), dict):
            # Whole-phase packets have no marker declaration; admit this
            # route only through the typed phase material checks above.
            result = phase_material_review(packet, context)
        else:
            # Preserve explicit coverage values.  A generic packet with an
            # empty or absent declaration is not allowed to become an empty
            # PASS merely because the fallback has no markers.
            required = None
            coverage_sources = (
                packet,
                context,
                context.get("assurance_packet") or {},
                context.get("task") or {},
                (context.get("artifact") or {}).get("body") or {},
            )
            for source in coverage_sources:
                if isinstance(source, dict) and "required_coverage" in source:
                    required = source["required_coverage"]
                    break
            if required is None:
                for source in coverage_sources[3:]:
                    if isinstance(source, dict) and "acceptance" in source:
                        required = source["acceptance"]
                        break
            findings = []
            subject = packet.get("subject")
            binding = packet.get("binding")
            if not isinstance(subject, str) or not subject:
                findings.append({"severity": "high", "statement": "The review subject is missing.",
                                 "evidence": "A reviewer cannot bind a judgment without a subject identity."})
            if not isinstance(binding, str) or not binding:
                findings.append({"severity": "high", "statement": "The immutable review binding is missing.",
                                 "evidence": "The packet did not provide a non-empty binding digest."})
            if (not isinstance(required, list) or
                    any(not isinstance(item, str) or not item for item in required) or
                    len(set(required)) != len(required)):
                findings.append({"severity": "high", "statement": "The review coverage declaration is missing or malformed.",
                                 "evidence": "required_coverage must be an explicitly declared list of unique marker strings."})
                required = required if isinstance(required, list) else []
            recognized_context = any(key in context for key in ("assurance_packet", "task", "traceability_packet", "candidate"))
            if isinstance(required, list) and not required and not recognized_context:
                findings.append({"severity": "high", "statement": "Unknown review material cannot receive empty coverage.",
                                 "evidence": "The packet supplied no typed subject context from which a zero-marker review could be derived."})

            root = Path.cwd()
            files = sorted(item for item in root.rglob("*")
                           if item.is_file() and ".git" not in item.parts)
            observations = [{"ref": subject or "missing-subject",
                             "detail": "Observed review subject binding=%s." % (binding or "missing")}] + [{"ref": str(item.relative_to(root)),
                             "detail": "Observed immutable review material size=%d sha256=%s" %
                                       (item.stat().st_size, hashlib.sha256(item.read_bytes()).hexdigest())}
                            for item in files[:40]]

            def add_implementation_ok(source):
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    return False
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "add":
                        continue
                    for child in ast.walk(node):
                        value = child.value if isinstance(child, ast.Return) else None
                        if (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add) and
                                isinstance(value.left, ast.Name) and isinstance(value.right, ast.Name) and
                                {value.left.id, value.right.id} == {"a", "b"}):
                            return True
                return False

            def integer_literal(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
                    return node.value
                if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
                        and isinstance(node.operand, ast.Constant)
                        and isinstance(node.operand.value, int)
                        and not isinstance(node.operand.value, bool)):
                    return -node.operand.value
                return None

            def test_calls_add(source):
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    return set()
                cases = set()
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
                        continue
                    call = node.test.left
                    if not isinstance(call, ast.Call):
                        continue
                    fn = call.func.id if isinstance(call.func, ast.Name) else (call.func.attr if isinstance(call.func, ast.Attribute) else None)
                    if fn != "add" or len(call.args) != 2:
                        continue
                    args = tuple(integer_literal(item) for item in call.args)
                    expected = (integer_literal(node.test.comparators[0])
                                if len(node.test.comparators) == 1 else None)
                    if any(value is None for value in args) or expected is None:
                        continue
                    cases.add((*args, expected))
                return cases

            calc_files = [item for item in files if item.name == "calc.py"]
            for calc in calc_files:
                if not add_implementation_ok(calc.read_text(encoding="utf-8")):
                    findings.append({"severity": "high", "statement": "The observed calculator does not implement exact addition.",
                                     "evidence": str(calc.relative_to(root)) + " has no add(a,b) return expression."})
                test = calc.parent / "test_calc.py"
                if not test.is_file():
                    findings.append({"severity": "high", "statement": "The calculator has no materialized executable test.",
                                     "evidence": str(test.relative_to(root)) + " is absent."})
                    continue
                calls = test_calls_add(test.read_text(encoding="utf-8"))
                required_cases = {(2, 3, 5), (-2, 1, -1)}
                if not required_cases.issubset(calls):
                    findings.append({"severity": "high", "statement": "The executable test does not call add for every required case.",
                                     "evidence": "%s observed calls=%s required=%s" %
                                                (str(test.relative_to(root)), sorted(calls), sorted(required_cases))})

            candidate = context.get("candidate")
            candidate_findings = candidate.get("findings", []) if isinstance(candidate, dict) else []
            dispositions = []
            for item in candidate_findings:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    findings.append({"severity": "high", "statement": "Candidate finding identity is malformed.",
                                     "evidence": "The reviewer cannot close an untyped finding."})
                    continue
                dispositions.append({"id": item["id"], "resolution": "acceptable",
                                     "reason": "The supplied material was inspected and no unresolved issue was found."})
            result = {"verdict": "pass" if not findings else "fail",
                      "rationale": "Finite reviewer validated packet identity, coverage, and materialized source/test evidence.",
                      "covered": required, "findings": findings, "observations": observations,
                      "dispositions": dispositions}
for index, value in enumerate(sys.argv):
    if value == "--output-last-message" and index + 1 < len(sys.argv):
        Path(sys.argv[index + 1]).write_text(json.dumps(result), encoding="utf-8")
        break
print(json.dumps({"type": "thread.started", "thread_id": "unit5-finite-governed"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}))
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _save_evidence(control: Control, record: dict) -> None:
    target = os.environ.get("U5_EVIDENCE_DIR")
    if not target:
        return
    directory = Path(target)
    directory.mkdir(parents=True, exist_ok=True)
    receipts = []
    for item in record["cases"]:
        row = control.s.one("SELECT body FROM receipts WHERE id=?", (item["receipt"],), True)
        assert row is not None
        body = parse_json(row["body"])
        receipts.append({
            "case": item["case"],
            "expected": item["expected"],
            "passed": item["passed"],
            "receipt": body,
        })
    (directory / "qualification_record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (directory / "qualification_receipts.json").write_text(
        json.dumps(receipts, indent=2, sort_keys=True), encoding="utf-8"
    )


def _run_finite_review(executable: Path, packet: dict, cwd: Path) -> dict:
    """Run one packet through the executable's real subprocess protocol."""
    cwd.mkdir(parents=True, exist_ok=True)
    output = cwd / "last-message.json"
    completed = subprocess.run(
        [str(executable), "--output-last-message", str(output)],
        input=json.dumps(packet), text=True, capture_output=True, cwd=cwd,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2
    event = json.loads(lines[-2])
    result = json.loads(event["item"]["text"])
    assert json.loads(output.read_text(encoding="utf-8")) == result
    return result


def _artifact_packet(*, role: str, artifact: dict, source: dict,
                     invariants: list[dict] | None = None) -> dict:
    policy_body = {"fixture": "finite artifact review policy"}
    policy = {"revision": 1, "digest": digest(policy_body), "body": policy_body}
    review_snapshot = {"format": "snapshot.v1", "digest": digest({"repos": {}})}
    context = {
        "review_policy": policy,
        "review_snapshot": review_snapshot,
        "sources": [source],
        "accepted_invariants": invariants or [],
    }
    material = {
        "artifact": {key: artifact[key] for key in
                     ("id", "project", "kind", "revision", "digest", "body")},
        "sources": context["sources"],
        "accepted_invariants": context["accepted_invariants"],
        "policy": policy,
    }
    binding = digest({
        "format": "daikibo.review-material.v1",
        "kind": "artifact_proposal",
        "project": artifact["project"],
        "subject": artifact["id"],
        "snapshot_digest": review_snapshot["digest"],
        "material": material,
    })
    return {
        "role": role,
        "subject": artifact["id"],
        "binding": binding,
        "context": {"artifact": artifact, **context},
    }


def test_finite_governed_subprocess_qualifies_all_catalog_cases(tmp_path):
    control = Control(tmp_path / "control", mode="governed", start_workers=False)
    try:
        control.owner = control.sec.authenticate(Path(control.sec.bootstrap()).read_text())
        project = control.k.create_project(control.owner, "Unit5 finite governed adapter")["id"]
        executable = tmp_path / "finite-codex"
        _write_finite_codex(executable)
        registration = control.rt.adapters.register(
            control.owner,
            "unit5-finite-governed-codex",
            "codex",
            str(executable),
            model="unit5-finite",
        )
        record = control.supervisor.qualify(
            control.owner, project, "unit5-finite-governed-codex"
        )

        expected = {case["id"]: case["expect"] for case in catalog()["cases"]}
        assert registration["simulated"] is False
        assert record["qualified"] is True
        assert {item["case"] for item in record["cases"]} == set(expected)
        assert all(item["passed"] for item in record["cases"])
        for item in record["cases"]:
            row = control.s.one("SELECT body FROM receipts WHERE id=?", (item["receipt"],), True)
            assert row is not None
            receipt = parse_json(row["body"])
            assert receipt["role"] == "adapter_qualification"
            assert receipt["assurance"] == "governed"
            assert receipt["readonly_verified"] is True
            assert receipt["judgment_valid"] is True
            assert receipt["result"]["verdict"] == expected[item["case"]]
            assert receipt["result"]["covered"] == ["AC-QUALIFY-" + item["case"]]
        adapter = control.rt.adapters.get("unit5-finite-governed-codex")
        assert adapter["qualified"] is True
        assert adapter["sha256"] == registration["sha256"]
        _save_evidence(control, record)
    finally:
        control.close()


def test_finite_artifact_branch_replays_positive_and_negative_material(tmp_path):
    executable = tmp_path / "finite-codex"
    _write_finite_codex(executable)
    source_content = "Both repositories expose exact integer addition."
    source = {
        "id": "SRC-finite-addition",
        "content": source_content,
        "digest": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
    }
    design_body = {
        "title": "Addition design",
        "statement": "Both repositories return the exact integer sum.",
        "responsibilities": ["sum in both repositories"],
        "failure_handling": "tests",
        "rollout": "git",
        "rejected_alternatives": ["subtract"],
        "source_refs": [source["id"]],
    }
    design = {
        "id": "DESIGN-finite-addition", "project": "PRJ-finite",
        "kind": "design", "revision": 1, "status": "accepted",
        "body": design_body,
        "digest": hashlib.sha256(json.dumps(
            design_body, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest(),
    }
    invariant = {"id": "REQ-finite-invariant", "statement": "Inputs are integers."}
    valid = _run_finite_review(
        executable, _artifact_packet(role="design", artifact=design,
                                     source=source, invariants=[invariant]), tmp_path,
    )
    assert valid["verdict"] == "pass"
    assert valid["covered"] == []
    assert {item["ref"] for item in valid["observations"]} >= {
        design["id"], source["id"], invariant["id"],
    }

    controls = []
    missing_source = _artifact_packet(role="design", artifact=design,
                                      source=source, invariants=[invariant])
    missing_source["context"]["sources"] = []
    controls.append(("missing-source", missing_source, {"blocked", "fail"}))

    changed_source = _artifact_packet(role="design", artifact=design,
                                      source=source, invariants=[invariant])
    changed_source["context"]["sources"][0]["content"] = "Foreign content."
    controls.append(("changed-source", changed_source, {"blocked", "fail"}))

    foreign_source = _artifact_packet(role="design", artifact=design,
                                      source=dict(source, project="PRJ-foreign"),
                                      invariants=[invariant])
    controls.append(("foreign-source", foreign_source, {"blocked", "fail"}))

    wrong_subject = _artifact_packet(role="design", artifact=design,
                                     source=source, invariants=[invariant])
    wrong_subject["subject"] = "DESIGN-foreign"
    controls.append(("wrong-subject", wrong_subject, {"blocked", "fail"}))

    legacy_body_binding = _artifact_packet(role="design", artifact=design,
                                           source=source, invariants=[invariant])
    legacy_body_binding["binding"] = design["digest"]
    controls.append(("legacy-artifact-digest-binding", legacy_body_binding,
                     {"blocked", "fail"}))

    changed_policy = _artifact_packet(role="design", artifact=design,
                                      source=source, invariants=[invariant])
    policy_body = changed_policy["context"]["review_policy"]["body"]
    policy_body["fixture"] = "different policy input"
    changed_policy["context"]["review_policy"]["digest"] = digest(policy_body)
    controls.append(("changed-policy-with-old-binding", changed_policy,
                     {"blocked", "fail"}))

    wrong_kind = _artifact_packet(role="consistency", artifact=design,
                                  source=source, invariants=[invariant])
    controls.append(("wrong-role-kind", wrong_kind, {"blocked", "fail"}))

    malformed_body = deepcopy(design)
    malformed_body["body"] = ["not", "an", "artifact", "body"]
    malformed = _artifact_packet(role="design", artifact=malformed_body,
                                 source=source, invariants=[invariant])
    controls.append(("malformed-body", malformed, {"blocked", "fail"}))

    subtracting = deepcopy(design)
    subtracting["body"]["responsibilities"] = ["subtract in both repositories"]
    subtracting["digest"] = hashlib.sha256(json.dumps(
        subtracting["body"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    controls.append(("contradictory-design", _artifact_packet(
        role="design", artifact=subtracting, source=source,
        invariants=[invariant],
    ), {"blocked", "fail"}))

    multiplying = deepcopy(design)
    multiplying["body"]["title"] = "Exact integer sum design"
    multiplying["body"]["responsibilities"] = [
        "Multiply the two inputs and return their product in both repositories",
    ]
    multiplying["digest"] = hashlib.sha256(json.dumps(
        multiplying["body"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    controls.append(("title-sum-multiply-responsibility", _artifact_packet(
        role="design", artifact=multiplying, source=source,
        invariants=[invariant],
    ), {"blocked", "fail"}))

    conflicting_invariant = _artifact_packet(
        role="design", artifact=design, source=source,
        invariants=[{"id": "INV-required-difference",
                     "statement": "Return subtraction of the two inputs; addition is prohibited."}],
    )
    controls.append(("conflicting-invariant", conflicting_invariant, {"blocked", "fail"}))

    unresolved_invariant = _artifact_packet(
        role="design", artifact=design, source=source,
        invariants=[{"id": "INV-unknown-constraint",
                      "statement": "Inputs are integers.",
                      "constraints": {"unmodeled_rule": "opaque"}}],
    )
    controls.append(("unresolved-invariant-constraint", unresolved_invariant, {"blocked", "fail"}))

    unknown_responsibility = deepcopy(design)
    unknown_responsibility["body"]["title"] = "Exact integer sum design"
    unknown_responsibility["body"]["responsibilities"] = ["Handle values safely."]
    unknown_responsibility["digest"] = hashlib.sha256(json.dumps(
        unknown_responsibility["body"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    controls.append(("unknown-responsibility", _artifact_packet(
        role="design", artifact=unknown_responsibility, source=source,
        invariants=[invariant],
    ), {"blocked", "fail"}))

    unknown = {"role": "design", "subject": "unknown", "binding": "x",
               "context": {"required_coverage": []}}
    controls.append(("unknown-empty-context", unknown, {"blocked", "fail"}))

    for name, packet, verdicts in controls:
        result = _run_finite_review(executable, packet, tmp_path / name)
        assert result["verdict"] in verdicts, (name, result)


def test_finite_artifact_branch_uses_public_knowledge_and_runtime_route(full, tmp_path):
    """The typed branch is reached from accepted public artifacts, not a fake packet."""
    project = full.k.create_project(full.owner, "Unit5 artifact branch")['id']
    source = full.k.source(
        full.owner, project,
        "Both repositories expose exact integer addition.",
    )
    invariant = full.k.propose(full.owner, project, "requirement", {
        "title": "Integer invariant", "statement": "Inputs are integers.",
        "acceptance": ["AC-INTEGER"], "source_refs": [source["id"]],
        "constraints": {"input_domain": "integer"},
    })
    full.k.accept(full.owner, invariant["id"], 1)
    design = full.k.propose(full.owner, project, "design", {
        "title": "Addition design",
        "statement": "Both repositories return the exact integer sum.",
        "responsibilities": ["sum in both repositories"],
        "failure_handling": "tests",
        "rollout": "git",
        "rejected_alternatives": ["subtract"], "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, design["id"], 1)
    interface = full.k.propose(full.owner, project, "interface", {
        "title": "Addition interface",
        "statement": "Both repositories consume the same integer addition boundary.",
        "input": "two integers", "output": "sum",
        "authentication": "none", "errors": "invalid integer input is rejected",
        "idempotency": "pure", "compatibility": "same integer sum",
        "consumers": ["repository-alpha", "repository-beta"],
        "verification": "pytest",
        "source_refs": [source["id"]],
    })
    full.k.accept(full.owner, interface["id"], 1)

    executable = tmp_path / "finite-codex"
    _write_finite_codex(executable)
    adapter = "unit5-finite-artifact-route"
    full.rt.adapters.register(full.owner, adapter, "codex", str(executable), model="unit5-finite")
    design_review = full.rt.review(full.owner, design["id"], "design", adapter)
    interface_review = full.rt.review(full.owner, interface["id"], "consistency", adapter)
    assert design_review["result"]["verdict"] == "pass"
    assert interface_review["result"]["verdict"] == "pass"
    assert design_review["result"]["covered"] == []
    assert interface_review["result"]["covered"] == []
    assert any(item["ref"] == design["id"] for item in design_review["result"]["observations"])
    assert any(item["ref"] == interface["id"] for item in interface_review["result"]["observations"])


def test_finite_phase_branch_requires_typed_material_and_rejects_unknown_empty_context(tmp_path):
    executable = tmp_path / "finite-codex"
    _write_finite_codex(executable)
    artifact_body = {"title": "Finite requirement", "statement": "Exact addition."}
    artifact_digest = hashlib.sha256(json.dumps(
        artifact_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    source_content = "Exact addition."
    source_digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    source_body = {"content": source_content, "dispositions": []}
    source_binding = hashlib.sha256(json.dumps(
        {"source_digest": source_digest, "dispositions": []},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    valid = {
        "role": "phase", "subject": "FLOW-finite", "binding": "b" * 64,
        "context": {
            "workflow": {
                "program": "FLOW-finite", "project": "PRJ-finite",
                "phase": "requirements", "revision": 1,
                "instructions": "Extract requirements.", "blockers": [],
                "next_operation": "Review and advance.", "binding": "b" * 64,
            },
            "artifacts": {"items": [{"id": "REQ-finite", "project": "PRJ-finite",
                                        "kind": "requirement",
                                        "revision": 1, "digest": artifact_digest,
                "body": artifact_body, "status": "accepted"}],
                          "next_offset": None},
            "sources": [{"id": "SRC-finite", "project": "PRJ-finite",
                         "digest": source_binding, "source_digest": source_digest,
                         "body": source_body}],
            "phase_contract": {"format": "phase-current-material.v1",
                               "phase": "requirements",
                               "semantic_scope": "requirements-source-coverage",
                               "supported": True},
            "required_current_proof": [
                {"kind": "accepted_requirement", "resolver": "Knowledge.artifact",
                 "project": "PRJ-finite", "phase": "requirements",
                 "status": "current", "required": True,
                 "subject_ref": {"kind": "artifact", "id": "REQ-finite",
                                 "project": "PRJ-finite", "revision": 1,
                                 "digest": artifact_digest, "artifact_kind": "requirement",
                                 "artifact_status": "accepted"},
                 "binding": artifact_digest},
                {"kind": "source_coverage", "resolver": "Knowledge.source_coverage",
                 "project": "PRJ-finite", "phase": "requirements",
                 "status": "current", "required": True,
                 "structurally_complete": True,
                 "sources": [{"source": "SRC-finite", "digest": source_digest,
                              "unclassified": []}]},
            ],
            "observed_evidence": [{"id": "EVD-finite", "run": "RUN-finite",
                                   "subject": "REQ-finite", "role": "requirements",
                                   "binding": artifact_digest, "project": "PRJ-finite",
                                   "result": {"verdict": "pass"}, "exit_code": 0,
                                   "assurance": "governed", "readonly_verified": True,
                                   "judgment_valid": True}],
            "typed_observations": [{"id": "EVD-finite", "run": "RUN-finite",
                                     "subject": "REQ-finite", "role": "requirements",
                                     "binding": artifact_digest, "project": "PRJ-finite",
                                     "result": {"verdict": "pass"}, "exit_code": 0,
                                     "assurance": "governed", "readonly_verified": True,
                                     "judgment_valid": True, "coverage": [],
                                     "subject_kind": "artifact",
                                     "resolver": "Knowledge.artifact",
                                     "subject_ref": {"kind": "artifact", "id": "REQ-finite",
                                                     "project": "PRJ-finite", "revision": 1,
                                                     "digest": artifact_digest,
                                                     "artifact_kind": "requirement"},
                                     "observation_state": "current", "current": True,
                                     "current_binding": artifact_digest}],
        },
    }
    result = _run_finite_review(executable, valid, tmp_path / "phase-valid")
    assert result["verdict"] == "pass"
    assert result["covered"] == []
    assert len(result["observations"]) == 3

    # A fresh project can satisfy the current requirements gate before any
    # non-phase receipt exists.  Empty history remains explicitly typed and
    # does not weaken the accepted-requirement/source-coverage proof.
    empty_history = deepcopy(valid)
    empty_history["context"]["observed_evidence"] = []
    empty_history["context"]["typed_observations"] = []
    result = _run_finite_review(executable, empty_history, tmp_path / "phase-empty-history")
    assert result["verdict"] == "pass", result
    assert any(item.get("detail") == "Consumed observed judgment population count=0."
               for item in result["observations"])

    empty_history_missing_proof = deepcopy(empty_history)
    empty_history_missing_proof["context"]["required_current_proof"] = []
    result = _run_finite_review(executable, empty_history_missing_proof,
                                tmp_path / "phase-empty-history-missing-proof")
    assert result["verdict"] == "blocked", result

    empty_history_stale_proof = deepcopy(empty_history)
    empty_history_stale_proof["context"]["required_current_proof"][0].update(binding="e" * 64)
    result = _run_finite_review(executable, empty_history_stale_proof,
                                tmp_path / "phase-empty-history-stale-proof")
    assert result["verdict"] == "blocked", result

    missing_sources = deepcopy(valid)
    missing_sources["context"]["sources"] = []
    result = _run_finite_review(executable, missing_sources, tmp_path / "phase-missing-source")
    assert result["verdict"] == "blocked"

    controls = [
        ("foreign-subject", lambda packet: packet.update(subject="FLOW-foreign")),
        ("foreign-project", lambda packet: packet["context"]["workflow"].update(project="PRJ-foreign")),
        ("stale-revision", lambda packet: packet["context"]["workflow"].update(revision=0)),
        ("foreign-binding", lambda packet: packet.update(binding="c" * 64)),
        ("artifact-digest", lambda packet: packet["context"]["artifacts"]["items"][0].update(digest="a" * 64)),
        ("source-digest", lambda packet: packet["context"]["sources"][0].update(source_digest="d" * 64)),
        ("non-list-history", lambda packet: packet["context"].update(observed_evidence=None)),
        ("non-list-typed-history", lambda packet: packet["context"].update(typed_observations=None)),
        ("stale-receipt", lambda packet: packet["context"]["observed_evidence"][0].update(binding="e" * 64)),
        ("unmapped-receipt", lambda packet: packet["context"]["observed_evidence"][0].update(subject="FOREIGN")),
        ("partial-current-proof", lambda packet: packet["context"]["observed_evidence"].clear()),
        ("stale-current-proof", lambda packet: packet["context"]["required_current_proof"][0].update(binding="e" * 64)),
        ("incomplete-current-proof", lambda packet: packet["context"]["required_current_proof"][1].update(structurally_complete=False)),
        ("foreign-current-proof-phase", lambda packet: packet["context"]["required_current_proof"][0].update(phase="design")),
        ("duplicate-source-proof", lambda packet: packet["context"]["required_current_proof"].append(deepcopy(packet["context"]["required_current_proof"][1]))),
        ("unhashable-source-proof", lambda packet: packet["context"]["required_current_proof"][1]["sources"][0].update(source=[])),
        ("unknown-phase-semantics", lambda packet: packet["context"]["phase_contract"].update(semantic_scope="all-phases")),
        ("foreign-typed-subject", lambda packet: packet["context"]["typed_observations"][0]["subject_ref"].update(project="PRJ-foreign")),
        ("corrupt-typed-subject", lambda packet: packet["context"]["typed_observations"][0].update(observation_state="corrupt")),
        ("unknown-typed-subject", lambda packet: packet["context"]["typed_observations"][0].update(subject_kind="unknown", resolver="unresolved", subject_ref={"kind":"unknown","id":"REQ-finite","project":"PRJ-finite"}, observation_state="unknown", current=False, current_binding=None)),
        ("resolver-kind-mismatch", lambda packet: packet["context"]["typed_observations"][0].update(resolver="Traceability.review_subject")),
        ("stale-typed-binding", lambda packet: packet["context"]["typed_observations"][0].update(current_binding="e" * 64)),
        ("receipt-coverage-mismatch", lambda packet: packet["context"]["typed_observations"][0].update(coverage=["invented"])),
        ("malformed-required-coverage", lambda packet: packet["context"]["typed_observations"][0]["subject_ref"].update(required_coverage=[[]])),
        ("current-failed-judgment", lambda packet: (packet["context"]["observed_evidence"][0]["result"].update(verdict="fail"), packet["context"]["typed_observations"][0]["result"].update(verdict="fail"))),
    ]
    for name, mutate in controls:
        case = deepcopy(valid)
        mutate(case)
        result = _run_finite_review(executable, case, tmp_path / ("phase-negative-" + name))
        must_block = name in {
            "stale-receipt", "unmapped-receipt", "partial-current-proof",
            "non-list-history", "non-list-typed-history",
            "stale-current-proof", "incomplete-current-proof", "foreign-current-proof-phase",
            "duplicate-source-proof", "unhashable-source-proof", "unknown-phase-semantics",
            "foreign-typed-subject",
            "corrupt-typed-subject", "unknown-typed-subject", "resolver-kind-mismatch",
            "stale-typed-binding", "receipt-coverage-mismatch", "malformed-required-coverage",
            "current-failed-judgment",
        }
        assert (result["verdict"] == "blocked" if must_block else
                result["verdict"] in {"fail", "blocked"}), (name, result)

    # BPACK/TREC/AOBJ/Task observations use their canonical typed resolver.
    # Their identifiers have no prefix dependency and their subjects do not
    # need to appear in the requirements artifact population.
    historical = deepcopy(valid)
    for kind in ("domain", "finding", "scenario", "test"):
        body = {"title": "Current %s" % kind,
                "statement": "Retained phase material."}
        historical["context"]["artifacts"]["items"].append({
            "id": "%s-current" % kind.upper(), "project": "PRJ-finite",
            "kind": kind, "revision": 1,
            "digest": hashlib.sha256(json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")).hexdigest(),
            "body": body, "status": "accepted",
        })
    history_specs = [
        ("opaque-breakdown", "breakdown_packet", "Breakdowns.review_subject", "design"),
        ("opaque-trace", "traceability_packet", "Traceability.review_subject", "trace"),
        ("opaque-assurance", "assurance_packet", "Assurance.review_subject_context", "requirements"),
        ("opaque-task", "task", "Governance.task_binding", "implementation"),
    ]
    for index, (subject_id, kind, resolver, role) in enumerate(history_specs):
        current_binding = chr(ord("f") - index) * 64
        ref = {"kind": kind, "id": subject_id, "project": "PRJ-finite",
               "binding": current_binding, "digest": current_binding,
               "required_coverage": []}
        if kind == "assurance_packet":
            ref["required_roles"] = [role]
        observed = {"id": "EVD-history-%d" % index, "run": "RUN-history-%d" % index,
                    "subject": subject_id, "role": role, "binding": current_binding,
                    "project": "PRJ-finite", "result": {"verdict": "pass", "covered": []},
                    "exit_code": 0, "assurance": "governed", "readonly_verified": True,
                    "judgment_valid": True}
        state = "historical" if kind == "task" else "current"
        typed = {**observed, "coverage": [], "subject_kind": kind, "resolver": resolver,
                 "subject_ref": ref, "observation_state": state, "current": state == "current",
                 "current_binding": current_binding}
        historical["context"]["observed_evidence"].append(observed)
        historical["context"]["typed_observations"].append(typed)
    result = _run_finite_review(executable, historical, tmp_path / "phase-historical-unmapped")
    assert result["verdict"] == "pass", result

    # A known PASS observation cannot substitute for phase-current proof.
    no_current_proof = deepcopy(historical)
    no_current_proof["context"]["required_current_proof"] = []
    result = _run_finite_review(executable, no_current_proof, tmp_path / "phase-history-is-not-proof")
    assert result["verdict"] == "blocked", result

    unknown_history = deepcopy(valid)
    unknown_receipt = {"id": "EVD-unknown", "run": "RUN-unknown",
                       "subject": "opaque-unresolved", "role": "trace",
                       "binding": "f" * 64, "project": "PRJ-finite",
                       "result": {"verdict": "pass", "covered": []}, "exit_code": 0,
                       "assurance": "governed", "readonly_verified": True,
                       "judgment_valid": True}
    unknown_history["context"]["observed_evidence"].append(unknown_receipt)
    unknown_history["context"]["typed_observations"].append({**unknown_receipt,
        "coverage": [], "subject_kind": "unknown", "resolver": "unresolved",
        "subject_ref": {"kind": "unknown", "id": "opaque-unresolved", "project": "PRJ-finite"},
        "observation_state": "unknown", "current": False, "current_binding": None})
    result = _run_finite_review(executable, unknown_history, tmp_path / "phase-unknown-history")
    assert result["verdict"] == "blocked", result

    unknown = {"role": "phase", "subject": "FLOW-unknown", "binding": "x",
               "context": {"required_coverage": []}}
    result = _run_finite_review(executable, unknown, tmp_path / "phase-unknown")
    assert result["verdict"] in {"blocked", "fail"}
