"""Controller derived reuse of technical node review receipts.

Unit 2b is intentionally a read only component.  It turns a selector list
into a sealed, controller-originated request bundle and then revalidates an
already observed Runtime review against the current semantic material.  The
module does not create pins, receipts, tasks, or new authority records.

The private seals below are an in-process component boundary.  They prevent a
JSON copy or a caller-created dictionary from being mistaken for a collector
result; they are not an authentication mechanism.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

from .agents import REVIEW_ROLES
from .assurance import PROFILE_NODE_SELECTORS, PROFILE_NODE_ROLE_SET
from .domain_responsibility import (NODE_V1, NODE_V2, DOMAIN_ROLE, DOMAIN_SELECTORS, DOMAIN_PACKET, responsibility_records, artifact_dependency_closure)
from .assurance_denominators import _test_plan_ref
from .assurance_relations import validate_typed_ref
from .common import Fault, canonical, digest, need, parse_json
from .obligations import review_task
from .observed_receipts import ordered_observed_receipts
from .task_revisions import task_definition_digest


NODE_RESULT_STATUSES = frozenset(
    {"satisfied", "missing", "stale", "failed", "unverified", "unsupported"}
)
_ARTIFACT_ROLES = {
    "requirements": {"requirement"},
    "design": {"design", "component"},
    "consistency": {"interface"},
    "test_plan": {"test"},
    DOMAIN_ROLE: {"domain"},
}
_REQUEST_KEYS = {
    "node_ref", "selector", "roles", "subject", "binding", "role_bindings",
    "required_coverage", "semantic_context_digest",
}
_NODE_RESULT_KEYS = {
    "node_ref", "selector", "subject", "binding",
    "semantic_context_digest", "roles",
}
_ROLE_RESULT_KEYS = {
    "status", "selected_receipt", "binding", "semantic_context_digest",
    "reason", "evidence_refs",
}
_REQUEST_ORIGIN = object()
_VALIDATED_ORIGIN = object()


def _json_copy(value: Any, name: str = "value") -> Any:
    try:
        return parse_json(canonical(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise Fault("invalid_node_request", f"{name} is not canonical JSON") from exc


def _invalid(message: str, details: Any = None) -> None:
    raise Fault("invalid_node_request", message, details)


def _unverified(message: str, details: Any = None) -> Fault:
    return Fault("unverified_node_review", message, details)


def _sha_digest(value: Any) -> str:
    """Digest a semantic JSON projection without retaining private material."""
    return digest(value)


class _NodeRequestBundle(list):
    """A list-shaped factory result with an immutable private origin token."""

    __slots__ = ("_token", "_control", "_project", "_seal", "_semantic", "_origin", "_contract")

    def __init__(self, values: Iterable[dict[str, Any]], *, control: Any,
                 project: str, semantic: dict[str, Any], contract: str = NODE_V1, _origin: object | None = None) -> None:
        if _origin is not _REQUEST_ORIGIN:
            _invalid("node request bundles can only be created by the controller factory")
        values = _json_copy(list(values), "node request bundle")
        list.__init__(self, values)
        object.__setattr__(self, "_token", object())
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_semantic", _json_copy(semantic, "node semantic material"))
        object.__setattr__(self, "_contract", contract)
        object.__setattr__(self, "_seal", self._make_seal())
        object.__setattr__(self, "_origin", _REQUEST_ORIGIN)

    def _make_seal(self) -> bytes:
        return canonical({
            "project": self._project,
            "requests": list(self),
            "semantic": self._semantic,
            "contract": self._contract,
        })

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("node request metadata is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            raise AttributeError("node request metadata is immutable")
        object.__delattr__(self, name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_NodeRequestBundle":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        list.__init__(copied, copy.deepcopy(list(self), memo))
        object.__setattr__(copied, "_token", self._token)
        object.__setattr__(copied, "_control", self._control)
        object.__setattr__(copied, "_project", self._project)
        object.__setattr__(copied, "_semantic", copy.deepcopy(self._semantic, memo))
        object.__setattr__(copied, "_contract", self._contract)
        object.__setattr__(copied, "_seal", self._seal)
        object.__setattr__(copied, "_origin", self._origin)
        return copied


class _ValidatedReviews(list):
    """A list-shaped, sealed output accepted by the criterion checker."""

    __slots__ = ("_token", "_control", "_actor", "_project", "_seal", "_requests", "_origin")

    def __init__(self, values: Iterable[dict[str, Any]], *, control: Any,
                 actor: Any, project: str, requests: _NodeRequestBundle,
                 _origin: object | None = None) -> None:
        if _origin is not _VALIDATED_ORIGIN:
            raise Fault("invalid_validated_reviews", "validated reviews can only come from node selection")
        values = _json_copy(list(values), "validated node reviews")
        list.__init__(self, values)
        object.__setattr__(self, "_token", object())
        object.__setattr__(self, "_control", control)
        object.__setattr__(self, "_actor", actor)
        object.__setattr__(self, "_project", project)
        object.__setattr__(self, "_requests", requests)
        object.__setattr__(self, "_seal", canonical(list(values)))
        object.__setattr__(self, "_origin", _VALIDATED_ORIGIN)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__ and hasattr(self, name):
            raise AttributeError("validated review metadata is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__slots__:
            raise AttributeError("validated review metadata is immutable")
        object.__delattr__(self, name)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_ValidatedReviews":
        copied = type(self).__new__(type(self))
        memo[id(self)] = copied
        list.__init__(copied, copy.deepcopy(list(self), memo))
        object.__setattr__(copied, "_token", self._token)
        object.__setattr__(copied, "_control", self._control)
        object.__setattr__(copied, "_actor", self._actor)
        object.__setattr__(copied, "_project", self._project)
        object.__setattr__(copied, "_requests", self._requests)
        object.__setattr__(copied, "_seal", self._seal)
        object.__setattr__(copied, "_origin", self._origin)
        return copied


def _verify_bundle(value: Any, control: Any) -> _NodeRequestBundle:
    if (not isinstance(value, _NodeRequestBundle) or value._control is not control or
            getattr(value, "_origin", None) is not _REQUEST_ORIGIN):
        _invalid("node_requests must be produced by build_node_requests")
    if value._seal != value._make_seal():
        _invalid("node request bundle was modified after collection")
    if not isinstance(value._project, str) or not value._project:
        _invalid("node request project is invalid")
    return value


def _verify_reviews(value: Any, control: Any) -> _ValidatedReviews:
    if (not isinstance(value, _ValidatedReviews) or value._control is not control or
            getattr(value, "_origin", None) is not _VALIDATED_ORIGIN):
        raise Fault("invalid_validated_reviews", "validated_reviews must come from select_node_reviews")
    _verify_bundle(value._requests, control)
    if len(value) != len(value._requests):
        raise Fault("invalid_validated_reviews", "validated review count differs from request count")
    for request, result in zip(value._requests, value):
        if (type(result) is not dict or result.get("node_ref") != request.get("node_ref") or
                result.get("selector") != request.get("selector") or
                result.get("subject") != request.get("subject") or
                result.get("binding") != request.get("binding")):
            raise Fault("invalid_validated_reviews", "validated review identity differs from its request")
        if (type(result.get("roles")) is not dict or
                set(result["roles"]) != set(request.get("roles", ()) )):
            raise Fault("invalid_validated_reviews", "validated review roles differ from its request")
    if value._seal != canonical(list(value)):
        raise Fault("invalid_validated_reviews", "validated reviews were modified after selection")
    return value


def _plain_ref(ref: dict[str, Any], project: str, *, expected: set[str] | None = None) -> dict[str, Any]:
    try:
        validate_typed_ref(ref, project=project, expected_kinds=expected)
    except Fault:
        raise
    # The normalizer returns a derived identity_digest.  It is a resolver
    # result, not part of the public typed-reference wire shape.
    return _json_copy(ref, "typed reference")


def _artifact_identity(row: dict[str, Any], project: str) -> dict[str, Any]:
    body = row.get("body")
    if isinstance(body, str):
        body = parse_json(body)
    if type(body) is not dict:
        raise Fault("integrity_error", "Artifact body is not an object", row.get("id"))
    if digest(body) != row.get("digest"):
        raise Fault("integrity_error", "Artifact body digest differs", row.get("id"))
    need(row.get("project") == project, "cross_project", "Artifact belongs to another project")
    need(isinstance(row.get("id"), str) and row["id"], "integrity_error", "Artifact id is missing")
    need(type(row.get("revision")) is int and row["revision"] >= 1,
         "integrity_error", "Artifact revision is invalid")
    need(isinstance(row.get("kind"), str) and row["kind"],
         "integrity_error", "Artifact kind is missing")
    # The SQL row is the authority.  A legacy body may omit these metadata
    # fields, but if it carries a self-claim it must not contradict the row.
    # We never use the claim to establish acceptance or selector membership.
    if "kind" in body and body["kind"] != row["kind"]:
        raise Fault("integrity_error", "Artifact body kind differs from canonical row", row["id"])
    if "status" in body and body["status"] != row.get("status"):
        raise Fault("integrity_error", "Artifact body status differs from canonical row", row["id"])
    return {
        "id": row["id"], "project": project, "kind": row["kind"],
        "revision": row["revision"], "digest": row["digest"],
        "body": _json_copy(body, "artifact body"),
    }


def _source_values(control: Any, project: str, source_refs: Any,
                   *, name: str = "source_refs") -> list[dict[str, Any]]:
    if source_refs is None:
        source_refs = []
    if type(source_refs) is not list:
        raise Fault("unverified_node_review", f"{name} is not a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_id in source_refs:
        if type(source_id) is not str or not source_id or source_id in seen:
            raise Fault("unverified_node_review", f"{name} contains an invalid or duplicate source")
        seen.add(source_id)
        row = control.s.one("SELECT * FROM sources WHERE id=? AND project=?", (source_id, project))
        if row is None:
            raise Fault("unverified_node_review", "Source is missing or belongs to another project", source_id)
        blob = row.get("blob")
        try:
            raw = control.s.blob_get(blob)
            if digest(raw) != blob:
                raise Fault("integrity_error", "Source CAS digest differs", source_id)
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Fault("unverified_node_review", "Source CAS is not UTF-8", source_id) from exc
        result.append({"id": source_id, "digest": blob, "content": content})
    return result


def _artifact_sources(control: Any, project: str, artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return _source_values(control, project, artifact["body"].get("source_refs", []),
                          name=f"artifact {artifact['id']} source_refs")


def _current_invariants(control: Any, project: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: dict[str, bytes] = {}
    for row in control.s.all(
            "SELECT id,project,kind,revision,digest,body,status FROM artifacts "
            "WHERE project=? AND status='accepted' ORDER BY id", (project,)):
        body = parse_json(row["body"])
        if digest(body) != row["digest"]:
            raise Fault("integrity_error", "Accepted artifact body digest differs", row["id"])
        if not (body.get("constraints") or body.get("critical")):
            continue
        constraints = body.get("constraints", {})
        if type(constraints) is not dict:
            raise Fault("integrity_error", "Invariant constraints are not an object", row["id"])
        entry = {"id": row["id"], "revision": row["revision"],
                 "digest": row["digest"], "statement": body.get("statement"),
                 "constraints": _json_copy(constraints, "invariant constraints")}
        if not isinstance(entry["statement"], str) or not entry["statement"]:
            raise Fault("integrity_error", "Invariant statement is missing", row["id"])
        encoded = canonical(entry)
        if row["id"] in seen and seen[row["id"]] != encoded:
            raise Fault("integrity_error", "Invariant identity is duplicated", row["id"])
        seen[row["id"]] = encoded
        entries.append(entry)
    entries.sort(key=lambda x: x["id"])
    return entries


def _without_self_invariant(entries: list[dict[str, Any]], artifact: dict[str, Any]) -> list[dict[str, Any]]:
    body = artifact["body"]
    if not (body.get("constraints") or body.get("critical")):
        return entries
    own = {"id": artifact["id"], "revision": artifact["revision"],
           "digest": artifact["digest"], "statement": body.get("statement"),
           "constraints": _json_copy(body.get("constraints", {}), "self invariant")}
    return [entry for entry in entries if entry != own]


def _prompt_invariants(value: Any, current: list[dict[str, Any]],
                       artifact: dict[str, Any]) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise Fault("unverified_node_review", "Prompt accepted_invariants is not a list")
    current_by_id = {entry["id"]: entry for entry in current}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    own_id = artifact["id"]
    for entry in value:
        if type(entry) is not dict or set(entry) != {"id", "revision", "digest", "statement", "constraints"}:
            raise Fault("unverified_node_review", "Prompt invariant shape is invalid")
        if entry["id"] in seen:
            raise Fault("unverified_node_review", "Prompt invariant identity is duplicated", entry.get("id"))
        seen.add(entry["id"])
        entry = _json_copy(entry, "prompt invariant")
        expected = current_by_id.get(entry["id"])
        if expected is None:
            # The artifact may have been accepted after the saved prompt was
            # produced.  Only the exact current self entry is allowed to be
            # absent/present across that transition.
            if entry["id"] != own_id:
                raise Fault("unverified_node_review", "Prompt invariant is not current", entry["id"])
        elif canonical(entry) != canonical(expected):
            raise Fault("stale_node_review", "Prompt invariant differs from current invariant", entry["id"])
        normalized.append(entry)
    normalized.sort(key=lambda x: x["id"])
    return normalized


def _artifact_semantic(control: Any, project: str, row: dict[str, Any],
                       *, prompt_context: dict[str, Any] | None = None) -> dict[str, Any]:
    artifact = _artifact_identity(row, project)
    current_invariants = _current_invariants(control, project)
    if prompt_context is None:
        sources = _artifact_sources(control, project, artifact)
        invariants = _without_self_invariant(current_invariants, artifact)
    else:
        prompt_artifact = prompt_context.get("artifact")
        if type(prompt_artifact) is not dict:
            raise Fault("unverified_node_review", "Prompt artifact context is missing")
        prompted = _artifact_identity(prompt_artifact, project)
        if prompted["id"] != artifact["id"] or prompted["kind"] != artifact["kind"]:
            raise Fault("stale_node_review", "Prompt artifact identity differs")
        # The full immutable body is part of the semantic input.  The current
        # row comparison below catches a changed revision or digest.
        sources_value = prompt_context.get("sources", [])
        if artifact["body"].get("source_refs", []) and "sources" not in prompt_context:
            raise Fault("unverified_node_review", "Prompt source material is missing")
        sources = _prompt_sources(control, project, artifact, sources_value)
        invariants = _prompt_invariants(prompt_context.get("accepted_invariants"),
                                        current_invariants, artifact)
        invariants = _without_self_invariant(invariants, artifact)
        if prompted != artifact:
            raise Fault("stale_node_review", "Prompt artifact material differs from current")
    return {"artifact": artifact, "sources": sources, "accepted_invariants": invariants}


def _prompt_sources(control: Any, project: str, artifact: dict[str, Any],
                    values: Any) -> list[dict[str, Any]]:
    refs = artifact["body"].get("source_refs", [])
    if type(values) is not list:
        raise Fault("unverified_node_review", "Prompt source material is not a list")
    if len(values) != len(refs):
        raise Fault("unverified_node_review", "Prompt source coverage is incomplete")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, source in enumerate(values):
        if type(source) is not dict or set(source) != {"id", "content", "digest"}:
            raise Fault("unverified_node_review", "Prompt source shape is invalid")
        source = _json_copy(source, "prompt source")
        if source["id"] in seen or source["id"] != refs[index]:
            raise Fault("unverified_node_review", "Prompt source order or identity differs", source.get("id"))
        seen.add(source["id"])
        if type(source["content"]) is not str or digest(source["content"].encode("utf-8")) != source["digest"]:
            raise Fault("unverified_node_review", "Prompt source content digest differs", source.get("id"))
        current = _source_values(control, project, [source["id"]], name="prompt sources")[0]
        if current != source:
            raise Fault("stale_node_review", "Prompt source differs from current", source["id"])
        result.append(source)
    return result


def _read_plan(control: Any, actor: Any, project: str,
               task: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    plan = control.s.one("SELECT * FROM plans WHERE task=?", (task["id"],))
    if plan is None:
        return None, None, "missing_test_plan"
    body = parse_json(plan["body"])
    if type(body) is not dict or digest(body) != plan["digest"]:
        raise Fault("integrity_error", "Frozen test plan digest differs", task["id"])
    unresolved: list[dict[str, Any]] = []
    ref = _test_plan_ref(control, actor, project, task, plan, unresolved)
    if ref is None:
        reason = unresolved[0].get("code") if unresolved else "missing_test_plan_material"
        return body, None, reason
    return body, ref, None


def _task_semantic(control: Any, actor: Any, project: str, task_row: dict[str, Any],
                   *, prompt_context: dict[str, Any] | None = None,
                   review_role: str | None = None) -> tuple[dict[str, Any], str | None, str | None, list[str]]:
    body = parse_json(task_row["body"])
    if type(body) is not dict:
        raise Fault("integrity_error", "Task definition body is not an object", task_row.get("id"))
    task_view = review_task(control.s, body)
    artifact_values = []
    for artifact_id in body.get("read_artifacts", []):
        try:
            row = control.k.artifact(actor, artifact_id)
        except Fault as exc:
            raise Fault("unverified_node_review", "Task read artifact is missing", artifact_id) from exc
        artifact = _artifact_identity(row, project)
        # Runtime's Task prompt contains the artifact view but not a separate
        # source section.  Validate source CAS now and retain only the exact
        # source identity in the semantic projection.
        sources = _artifact_sources(control, project, artifact)
        artifact_values.append({"artifact": artifact,
                                "source_refs": [x["id"] for x in sources],
                                "source_digests": [x["digest"] for x in sources]})
    plan_body, plan_ref, plan_reason = _read_plan(control, actor, project, task_row)
    semantic = {"task": _json_copy(task_view, "task review material"),
                "read_artifacts": artifact_values,
                "test_plan": _json_copy(plan_body, "test plan") if plan_body is not None else None}
    plan_review_material = None
    if plan_body is not None and review_role == "test_plan":
        materials = getattr(control.g, "review_materials", None)
        if materials is None:
            raise Fault("unverified_node_review", "Current test-plan review material is unavailable")
        snapshot_override = None
        if prompt_context is not None:
            baseline = prompt_context.get("review_snapshot")
            if (type(baseline) is not dict or type(baseline.get("digest")) is not str or
                    type(baseline.get("format")) is not str):
                raise Fault("unverified_node_review", "Prompt test-plan baseline identity is missing")
            snapshot_override = {"format": baseline["format"], "repos": {},
                                 "digest": baseline["digest"]}
        else:
            plan_row = control.s.one("SELECT * FROM plans WHERE task=?", (task_row["id"],))
            snapshot_override = materials.frozen_plan_snapshot(actor, task_row, plan_row)
            need(snapshot_override is not None, "unverified_node_review",
                 "Current frozen test-plan baseline is unavailable or stale")
        frozen = materials.test_plan(actor, task_row, plan_body,
                                     store_snapshot_blobs=False,
                                     snapshot_override=snapshot_override)
        plan_review_material = frozen
        for key in ("review_policy", "review_snapshot", "task_reads", "dependencies"):
            semantic[key] = frozen["context"][key]
    if prompt_context is not None:
        if set(prompt_context) - {"task", "read_artifacts", "test_plan", "managed_execution", "read_access", "candidate", "test_evidence",
                                  "review_policy", "review_snapshot", "task_reads", "dependencies"}:
            raise Fault("unverified_node_review", "Prompt task context has unknown semantic fields")
        prompted_task = prompt_context.get("task")
        prompted_plan = prompt_context.get("test_plan")
        prompted_reads = prompt_context.get("read_artifacts")
        if prompted_task != semantic["task"] or prompted_plan != semantic["test_plan"]:
            raise Fault("stale_node_review", "Prompt Task or test plan material differs")
        if plan_review_material is not None:
            for key in ("review_policy", "review_snapshot", "task_reads", "dependencies"):
                if prompt_context.get(key) != plan_review_material["context"][key]:
                    raise Fault("stale_node_review", "Prompt test-plan review material differs", key)
        if type(prompted_reads) is not list or len(prompted_reads) != len(artifact_values):
            raise Fault("unverified_node_review", "Prompt Task read artifact coverage is incomplete")
        prompted_values = []
        for index, value in enumerate(prompted_reads):
            if type(value) is not dict:
                raise Fault("unverified_node_review", "Prompt Task read artifact is malformed")
            # Ignore Runtime's mutable row fields, but never allow a different
            # immutable identity/body to be projected away.
            prompted_artifact = value
            if "artifact" in value and type(value["artifact"]) is dict:
                prompted_artifact = value["artifact"]
            prompted_identity = _artifact_identity(prompted_artifact, project)
            expected = artifact_values[index]["artifact"]
            if prompted_identity != expected:
                raise Fault("stale_node_review", "Prompt read artifact differs")
            prompted_values.append({"artifact": prompted_identity,
                                    "source_refs": expected["body"].get("source_refs", []),
                                    "source_digests": artifact_values[index]["source_digests"]})
        if prompted_values != artifact_values:
            raise Fault("stale_node_review", "Prompt read artifact source material differs")
    return semantic, plan_ref, plan_reason, list(task_view.get("acceptance", []))


def _selectors(contract):
    need(contract in {NODE_V1, NODE_V2}, "invalid_node_request", "Unknown node contract")
    return {**PROFILE_NODE_SELECTORS, **DOMAIN_SELECTORS} if contract == NODE_V2 else PROFILE_NODE_SELECTORS


def domain_review_material(control, actor, project, row):
    """Exact controller material shared by Runtime producer and N consumer."""
    need(row["kind"] == "domain" and row["status"] == "accepted",
         "invalid_node_request", "DOMAIN review requires a current accepted DOMAIN")
    artifact = _artifact_identity(row, project)
    ref = {"kind":"artifact", "project":project, "artifact":artifact["id"],
           "revision":artifact["revision"], "body_digest":artifact["digest"]}
    def resolve(dependency):
        dep = control.k.artifact(actor, dependency["artifact"])
        need(dep["project"] == project and dep["status"] == "accepted" and
             dep["revision"] == dependency["revision"] and dep["digest"] == dependency["body_digest"],
             "stale_reference", "DOMAIN semantic dependency is not current")
        return dep
    records, dependencies = responsibility_records(ref, "domain", artifact["body"], resolver=resolve)
    dependencies, source_rows = artifact_dependency_closure(ref, resolve,
        lambda ident:control.s.one("SELECT * FROM sources WHERE id=? AND project=?", (ident,project)))
    sources = _source_values(control, project, [x["id"] for x in source_rows], name="DOMAIN source closure")
    fields = ["responsibilities", "non_responsibilities", "owned_data", "interfaces"]
    if "structural_obligations" in artifact["body"]:
        fields.append("structural_obligations")
    markers = [x["id"] for x in records]
    for field in fields:
        need(field in artifact["body"], "invalid_node_request", "DOMAIN boundary field is missing", field)
        markers.append("domain-field:" + digest({"source_ref":ref, "pointer":"/"+field,
                                                "value_digest":digest(artifact["body"][field])}))
    return {"format":DOMAIN_PACKET, "node_contract":NODE_V2, "node_ref":ref,
            "artifact":artifact, "sources":sources, "accepted_invariants":_current_invariants(control, project),
            "responsibility_obligations":records, "dependency_refs":sorted(dependencies, key=canonical),
            "required_coverage":sorted(set(markers))}


def _review_roles(selector: str, roles: list[str] | None, contract: str = NODE_V1) -> list[str]:
    """Validate the review-role projection for one exact node selector.

    A profile selector identifies the subject population while its declared
    roles identify the independent reviews required for that subject.  Older
    callers omit ``roles`` and therefore retain the selector's minimum role.
    The Unit 3 evaluator supplies the complete profile role list here.
    """
    minimum = _selectors(contract).get(selector)
    if minimum is None and selector in set(_selectors(contract).values()):
        minimum = selector
    if minimum is None:
        _invalid("Unknown node selector", selector)
    if roles is None:
        return [minimum]
    if (type(roles) is not list or not roles or roles != sorted(set(roles)) or
            any(type(role) is not str or role not in (PROFILE_NODE_ROLE_SET | {DOMAIN_ROLE} if contract == NODE_V2 else PROFILE_NODE_ROLE_SET) for role in roles) or
            minimum not in roles):
        _invalid("Node review roles are not a canonical complete list", {"selector": selector, "roles": roles})
    return list(roles)


def _node_snapshot(control: Any, actor: Any, project: str,
                   selector: str, node_ref: dict[str, Any],
                   review_roles: list[str] | None = None, contract: str = NODE_V1) -> dict[str, Any]:
    role = _selectors(contract).get(selector)
    if role is None and selector in set(_selectors(contract).values()):
        role = selector
    if role is None:
        _invalid("Unknown node selector", selector)
    roles = _review_roles(selector, review_roles, contract)
    ref = _plain_ref(node_ref, project)
    if ref["kind"] == "artifact":
        try:
            row = control.k.artifact(actor, ref["artifact"])
        except Fault as exc:
            raise Fault("unresolved_reference", "Artifact is missing", ref["artifact"]) from exc
        if row["revision"] != ref["revision"] or row["digest"] != ref["body_digest"]:
            raise Fault("stale_reference", "Artifact reference is not current", ref["artifact"])
        if row["status"] != "accepted":
            raise Fault("unresolved_reference", "Artifact is not accepted", ref["artifact"])
        if row["kind"] not in _ARTIFACT_ROLES[role]:
            raise Fault("invalid_node_request", "Artifact kind does not match selector", {"kind": row["kind"], "selector": selector})
        artifact = _artifact_identity(row, project)
        semantic = (domain_review_material(control, actor, project, row) if role == DOMAIN_ROLE
                    else _artifact_semantic(control, project, row))
        if contract == NODE_V2 and role != DOMAIN_ROLE:
            semantic = {"node_contract":NODE_V2, "material":semantic}
        coverage = semantic["required_coverage"] if role == DOMAIN_ROLE else artifact["body"].get("acceptance", [])
        if type(coverage) is not list or any(type(item) is not str or not item for item in coverage):
            raise Fault("integrity_error", "Artifact acceptance coverage is malformed", artifact["id"])
        request = {"node_ref": ref, "selector": selector, "roles": roles,
                   "subject": artifact["id"], "binding": artifact["digest"],
                   "role_bindings": {
                       review_role: (artifact["digest"] if review_role == DOMAIN_ROLE else
                                     control.g.review_materials.artifact(actor, artifact["id"], review_role)["binding"])
                       for review_role in roles
                   },
                   "required_coverage": _json_copy(coverage, "artifact coverage"),
                   "semantic_context_digest": _sha_digest(semantic)}
        return {"request": request, "semantic": semantic, "kind": "domain" if role == DOMAIN_ROLE else "artifact",
                "contract": contract,
                "material_ok": True, "material_reason": None}
    if ref["kind"] != "task_revision" or role != "test_plan":
        raise Fault("invalid_node_request", "Node selector and reference kind do not match")
    row = control.s.one("SELECT * FROM tasks WHERE id=? AND project=?", (ref["task"], project))
    if row is None:
        raise Fault("unresolved_reference", "Task is missing", ref["task"])
    body = parse_json(row["body"])
    actual_definition = task_definition_digest(body)
    if row["revision"] != ref["revision"] or actual_definition != ref["definition_digest"]:
        raise Fault("stale_reference", "Task revision is not current", ref["task"])
    semantic, plan_ref, plan_reason, coverage = _task_semantic(
        control, actor, project, row, review_role="test_plan")
    if semantic["test_plan"] is None:
        binding = digest({})
    else:
        binding = digest(semantic["test_plan"])
    role_bindings = {}
    for review_role in roles:
        if review_role == "test_plan" and semantic["test_plan"] is not None:
            baseline = semantic["review_snapshot"]
            role_bindings[review_role] = control.g.review_materials.test_plan(
                actor, row, semantic["test_plan"], store_snapshot_blobs=False,
                snapshot_override={"format": baseline["format"], "repos": {},
                                  "digest": baseline["digest"]},
            )["binding"]
        else:
            role_bindings[review_role] = control.g.task_binding(row["id"])
    if contract == NODE_V2:
        semantic = {"node_contract":NODE_V2, "material":semantic}
    request = {"node_ref": ref, "selector": selector, "roles": roles,
               "subject": row["id"], "binding": binding,
               "role_bindings": role_bindings,
               "required_coverage": _json_copy(coverage, "Task coverage"),
               "semantic_context_digest": _sha_digest(semantic)}
    return {"request": request, "semantic": semantic, "kind": "task_plan", "contract":contract,
            "material_ok": plan_ref is not None, "material_reason": plan_reason,
            "plan_ref": plan_ref}


def build_node_requests(control: Any, actor: Any, *, project: str,
                        selectors: list[dict[str, Any]], contract: str = NODE_V1) -> list[dict[str, Any]]:
    """Collect current node requests from controller rows.

    The returned object intentionally remains list-shaped for callers that
    already consume the internal API, while its private seal prevents a raw
    JSON list from crossing the component boundary.
    """
    if type(project) is not str or not project or "\x00" in project:
        _invalid("project is invalid")
    if type(selectors) is not list:
        _invalid("selectors must be a list")
    # Authorize the read through the existing controller service.  No raw
    # actor/project string is treated as authority by the collector itself.
    control.k.project(actor, project)
    requests: list[dict[str, Any]] = []
    semantic: dict[str, Any] = {}
    seen: set[bytes] = set()
    for item in selectors:
        if type(item) is not dict or set(item) not in ({"selector", "node_ref"}, {"selector", "node_ref", "roles"}):
            _invalid("selector entries must contain selector, node_ref, and optional roles")
        selector = item["selector"]
        if type(selector) is not str or selector not in _selectors(contract):
            _invalid("selector is not in _selectors(contract)", selector)
        roles = _review_roles(selector, item.get("roles"), contract)
        # Validate before using the reference in an identity key.  The raw
        # ref stays exact; derived identity_digest is never persisted here.
        ref = _plain_ref(item["node_ref"], project)
        role = _selectors(contract)[selector]
        key = canonical({"selector": selector, "node_ref": ref, "roles": roles})
        if key in seen:
            continue
        seen.add(key)
        info = _node_snapshot(control, actor, project, selector, ref, roles, contract)
        request = info["request"]
        requests.append(request)
        semantic[digest({"selector": selector, "roles": roles, "node_ref": ref})] = {
            "semantic": info["semantic"], "kind": info["kind"],
            "material_ok": info["material_ok"], "material_reason": info["material_reason"],
        }
    return _NodeRequestBundle(requests, control=control, project=project, semantic=semantic, contract=contract,
                              _origin=_REQUEST_ORIGIN)


def _status_result(request: dict[str, Any], *, status: str, reason: str,
                   receipt: dict[str, Any] | None = None,
                   semantic_digest: str | None = None,
                   evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if status not in NODE_RESULT_STATUSES:
        raise AssertionError(status)
    selected = None
    refs = evidence or []
    if receipt is not None:
        selected = {"id": receipt["id"], "digest": digest(receipt)}
        refs = [{"kind": "receipt", "id": receipt["id"], "digest": digest(receipt)}]
    return {"status": status, "selected_receipt": selected,
            "binding": request["binding"],
            "semantic_context_digest": semantic_digest or request["semantic_context_digest"],
            "reason": reason, "evidence_refs": refs}


def _prompt_from_receipt(control: Any, receipt: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    input_digest = receipt.get("input_digest")
    if type(input_digest) is not str:
        raise _unverified("receipt input digest is missing")
    if run.get("input_digest") != input_digest:
        raise _unverified("run and receipt input digest differ")
    try:
        raw = control.s.blob_get(input_digest)
        if digest(raw) != input_digest:
            raise _unverified("review prompt CAS digest differs")
        text = raw.decode("utf-8")
        prompt = parse_json(text)
    except UnicodeDecodeError as exc:
        raise _unverified("review prompt is not UTF-8") from exc
    except Fault as exc:
        if exc.code in {"missing_blob", "not_found", "integrity_error"}:
            raise _unverified("review prompt CAS is unavailable") from exc
        raise
    if type(prompt) is not dict or set(prompt) != {"role", "subject", "binding", "context", "instructions", "schema"}:
        raise _unverified("review prompt shape is unknown")
    if prompt["role"] != receipt.get("role") or prompt["subject"] != receipt.get("subject") or prompt["binding"] != receipt.get("binding"):
        raise _unverified("review prompt identity differs from receipt")
    if type(prompt["instructions"]) is not str or type(prompt["schema"]) is not dict:
        raise _unverified("review prompt instructions/schema shape is unknown")
    if type(prompt["context"]) is not dict:
        raise _unverified("review prompt context is not an object")
    return prompt


def _receipt_candidates(control: Any, request: dict[str, Any], role: str) -> tuple[list[str], int | None]:
    # Governance supplies the candidate family and current binding. The shared
    # reader validates receipt/run identity and chooses the latest observation
    # by the durable run_observed event sequence, never by created telemetry.
    binding = request.get("role_bindings", {}).get(role, request["binding"])
    ids = control.g.evidence_for(request["subject"], binding, role)
    if not ids:
        return [], None
    ordered = ordered_observed_receipts(
        control, project=request["node_ref"]["project"], subject=request["subject"],
        role=role, binding=binding,
        receipt_ids=[item["id"] if isinstance(item, dict) else item for item in ids],
    )
    if not ordered:
        return [], None
    usable = [item for item in ordered if control.g._usable_review_judgment(item["body"])]
    latest = (usable or ordered)[-1]
    return [latest["row"]["id"]], latest["event_seq"]


def _review_one(control: Any, actor: Any, request: dict[str, Any], info: dict[str, Any], role: str) -> dict[str, Any]:
    if not info["material_ok"]:
        return _status_result(request, status="unverified", reason=info["material_reason"] or "missing_material")
    try:
        ids, _ = _receipt_candidates(control, request, role)
    except Fault as exc:
        return _status_result(request, status="unverified", reason=exc.code)
    if not ids:
        return _status_result(request, status="unverified", reason="no_current_receipt")
    if len(ids) > 1:
        return _status_result(request, status="unverified", reason="ambiguous_latest_receipts")
    ident = ids[0]
    receipt: dict[str, Any] | None = None
    try:
        receipt = control.g.receipt(ident)
        run_row = control.s.one("SELECT * FROM runs WHERE id=?", (receipt["run"],))
        if run_row is None:
            raise _unverified("review run is missing")
        run_body = parse_json(run_row["body"])
        if run_row["project"] != request["node_ref"]["project"] or run_row["role"] != role:
            raise _unverified("review run identity differs")
        # Existing Governance remains authoritative for signatures, result,
        # readonly state, qualification and task test-evidence currentness.
        try:
            binding = request.get("role_bindings", {}).get(role, request["binding"])
            qualified = control.g.require_review(ident, request["subject"], binding, {role}, latest=True)
        except Fault as exc:
            if receipt.get("result", {}).get("verdict") != "pass" or receipt.get("failure") or receipt.get("exit_code") != 0:
                return _status_result(request, status="failed", reason=exc.code,
                                      receipt=receipt)
            return _status_result(request, status="unverified", reason=exc.code,
                                  receipt=receipt)
        prompt = _prompt_from_receipt(control, receipt, run_body)
        result = receipt.get("result")
        if type(result) is not dict:
            return _status_result(request, status="unverified", reason="invalid_review_result", receipt=receipt)
        covered = result.get("covered")
        if type(covered) is not list or any(type(x) is not str for x in covered):
            return _status_result(request, status="unverified", reason="invalid_review_coverage", receipt=receipt)
        supplemental_domain_role = info["kind"] == "domain" and role != DOMAIN_ROLE
        coverage = (info["semantic"]["artifact"]["body"].get("acceptance", [])
                    if supplemental_domain_role else request["required_coverage"])
        if any(marker not in covered for marker in coverage):
            return _status_result(request, status="unverified", reason="required_coverage_missing", receipt=receipt)
        if result.get("findings") != []:
            return _status_result(request, status="unverified", reason="review_findings_present", receipt=receipt)
        role_context = prompt["context"]
        if supplemental_domain_role:
            # Additional roles retain their own artifact-review semantics.
            # They never satisfy the mandatory DOMAIN role or acquire its
            # responsibility markers by relabeling an old receipt.
            current_row = control.k.artifact(actor, request["subject"])
            current_semantic = _artifact_semantic(control, request["node_ref"]["project"], current_row)
            prompted_semantic = _artifact_semantic(control, request["node_ref"]["project"], current_row,
                                                   prompt_context=role_context)
            if prompted_semantic != current_semantic:
                return _status_result(request, status="stale", reason="semantic_context_changed", receipt=receipt)
            return _status_result(request, status="satisfied", reason="supplemental_artifact_review_accepted",
                                  receipt=qualified)
        if info["kind"] == "domain":
            if covered != request["required_coverage"]:
                return _status_result(request, status="unverified", reason="domain_coverage_not_exact", receipt=receipt)
            prompted_semantic = role_context.get("domain_review")
            if prompted_semantic != info["semantic"]:
                return _status_result(request, status="stale", reason="domain_semantic_context_changed", receipt=receipt)
        elif info["kind"] == "artifact":
            prompted_semantic = _artifact_semantic(
                control, request["node_ref"]["project"],
                control.k.artifact(actor, request["subject"]),
                prompt_context=role_context)
        else:
            task_row = control.s.one("SELECT * FROM tasks WHERE id=?", (request["subject"],))
            prompted_semantic, _, _, _ = _task_semantic(
                control, actor, request["node_ref"]["project"], task_row,
                prompt_context=role_context, review_role=role)
            if role != "test_plan":
                # Supplemental Task roles use Runtime's ordinary Task review
                # projection. The plan selector's additional snapshot, policy,
                # read-pin and dependency fields belong only to test_plan
                # receipts; _task_semantic has already checked this role's
                # exact Task/plan/reads prompt, while Governance checked its
                # current test-evidence selection and task binding.
                return _status_result(request, status="satisfied",
                                      reason="supplemental_task_review_accepted",
                                      receipt=qualified)
        if info.get("contract") == NODE_V2 and info["kind"] != "domain":
            prompted_semantic = {"node_contract":NODE_V2, "material":prompted_semantic}
        prompted_digest = _sha_digest(prompted_semantic)
        if prompted_digest != request["semantic_context_digest"]:
            return _status_result(request, status="stale", reason="semantic_context_changed",
                                  receipt=receipt, semantic_digest=prompted_digest)
        return _status_result(request, status="satisfied", reason="review_accepted",
                              receipt=qualified, semantic_digest=prompted_digest)
    except Fault as exc:
        if exc.code in {"review_failed", "agent_failed", "invalid_review"}:
            return _status_result(request, status="failed", reason=exc.code, receipt=receipt)
        return _status_result(request, status="unverified", reason=exc.code, receipt=receipt)
    except (KeyError, TypeError, ValueError, UnicodeError):
        return _status_result(request, status="unverified", reason="invalid_saved_review_material", receipt=receipt)


def select_node_reviews(control: Any, actor: Any, *,
                        node_requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select exactly the latest current receipt for each sealed request."""
    bundle = _verify_bundle(node_requests, control)
    control.k.project(actor, bundle._project)
    outputs: list[dict[str, Any]] = []
    for request in bundle:
        if type(request) is not dict or set(request) != _REQUEST_KEYS:
            _invalid("NodeRequest has unknown or missing fields")
        if type(request.get("roles")) is not list or request["roles"] != sorted(set(request["roles"])):
            _invalid("NodeRequest roles are not canonical")
        role = request["selector"]
        if role not in _selectors(bundle._contract):
            _invalid("NodeRequest selector is unknown", role)
        expected_roles = _review_roles(role, request["roles"], bundle._contract)
        if request["roles"] != expected_roles:
            _invalid("NodeRequest roles do not include the selector minimum")
        try:
            current = _node_snapshot(control, actor, bundle._project, role, request["node_ref"], request["roles"], bundle._contract)
        except Fault as exc:
            status = "stale" if exc.code in {"stale_reference", "stale_node_review"} else "unverified"
            role_results = {
                review_role: _status_result(request, status=status, reason=exc.code)
                for review_role in request["roles"]
            }
            outputs.append({"node_ref": request["node_ref"], "selector": role,
                            "subject": request["subject"], "binding": request["binding"],
                            "semantic_context_digest": request["semantic_context_digest"],
                            "roles": role_results})
            continue
        current_request = current["request"]
        if current_request != request:
            role_results = {
                review_role: _status_result(request, status="stale", reason="node_request_is_stale",
                                            semantic_digest=current_request["semantic_context_digest"])
                for review_role in request["roles"]
            }
        else:
            role_results = {
                review_role: _review_one(control, actor, request, current, review_role)
                for review_role in request["roles"]
            }
        outputs.append({"node_ref": request["node_ref"], "selector": role,
                        "subject": request["subject"], "binding": request["binding"],
                        "semantic_context_digest": current_request["semantic_context_digest"],
                        "roles": role_results})
    return _ValidatedReviews(outputs, control=control, actor=actor, project=bundle._project, requests=bundle,
                             _origin=_VALIDATED_ORIGIN)


def validated_reviews_origin(value: Any, control: Any) -> dict[str, Any]:
    """Internal adapter used by ``assurance_criteria`` without exposing a new wire API."""
    bundle = _verify_reviews(value, control)
    return {"project": bundle._project, "items": list(bundle), "requests": bundle._requests,
            "control": bundle._control, "actor": bundle._actor}


__all__ = ["build_node_requests", "select_node_reviews", "validated_reviews_origin"]
