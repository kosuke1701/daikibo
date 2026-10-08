"""Deterministic protocol fixture for assurance gate tests only."""

import json
import sys


request = json.load(sys.stdin)
context = request.get("context", {})
scope_packets = []


def walk(value):
    if isinstance(value, dict):
        scope = value.get("scope_review")
        if isinstance(scope, dict) and scope.get("format") == "change-scope-review.v1":
            scope_packets.append((value, scope))
        for child in value.values():
            walk(child)
    elif isinstance(value, list):
        for child in value:
            walk(child)


walk(context)
dispositions = []
observations = [{"ref": request.get("subject"), "detail": "Fixture read the packet markers."}]
seen = set()
for material, scope in scope_packets:
    layer = scope.get("layer", "local_repair")
    after_by = {item.get("artifact"): item for item in material.get("before_after", [])}
    effects = {}
    for item in scope.get("required_dispositions", []):
        if item.get("kind") != "delta_effect":
            continue
        artifact = item.get("subject")
        detail = after_by.get(artifact, {})
        before = detail.get("before", {}).get("body", {})
        after = detail.get("after", {}).get("body", {})
        changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
        preserved = detail.get("after", {}).get("status") != "withdrawn" and (
            not changed or changed <= {"title"}
        )
        if preserved:
            effects[artifact] = "preserves_meaning"
            observations.append({"ref": artifact,
                                 "detail": "Fixture compared this exact before/after and source; test-only judgment."})
        else:
            effects[artifact] = "changes_upper_contract"
    unresolved = bool(scope.get("unknown_neighbors"))
    upper = any(value in {"changes_upper_contract", "unknown"} for value in effects.values())
    layer_scope = "unresolved" if unresolved else "upper_scope_required" if upper else "within_scope"
    target = "awaiting_product_decision" if layer_scope == "upper_scope_required" else layer
    for item in scope.get("required_dispositions", []):
        marker = item["id"]
        if marker in seen:
            continue
        seen.add(marker)
        kind = item.get("kind")
        if kind == "layer_scope":
            resolution = layer_scope
        elif kind == "layer_target":
            resolution = target
        elif kind == "delta_effect":
            resolution = effects[item["subject"]]
        elif kind == "review_carry_and_task_fence":
            resolution = "unaffected" if all(v == "preserves_meaning" for v in effects.values()) else "affected"
        elif kind == "interface_consumer":
            resolution = "addressed"
        elif kind == "declared_unknown_consumer":
            resolution = "unresolved"
        else:
            continue
        dispositions.append({"id": marker, "resolution": resolution,
                             "reason": "Deterministic test fixture only."})
print(json.dumps({
    "verdict": "pass",
    "rationale": "Deterministic assurance protocol fixture; not semantic acceptance.",
    "covered": context.get("required_coverage", []),
    "findings": [],
    "observations": observations,
    "dispositions": dispositions,
}))
