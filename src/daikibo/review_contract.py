"""Shared review-output schemas and typed execution-control dispositions.

The general review schema is intentionally permissive for legacy review roles.
Execution-control reviews select a copy with the finite vocabulary that the
application gate already enforces.  Keeping the vocabulary and instructions in
one small component prevents the prompt and provider schemas from drifting.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .common import need


ATTEMPT_RESOLUTIONS = ("progress", "no_progress", "inconclusive")
CONTROL_RESOLUTIONS = ("approved", "rejected", "inconclusive")
EXECUTION_CONTROL_RESOLUTIONS = ATTEMPT_RESOLUTIONS + ("approved", "rejected")

PASS_DESCRIPTION = "Use pass only when findings=[] (an empty array); unresolved findings require fail or blocked."
GENERIC_FINDINGS_DESCRIPTION = (
    "Unresolved problems with the review subject or decision under review. "
    "A pass verdict requires an empty findings array."
)
GENERIC_OBSERVATIONS_DESCRIPTION = (
    "Evidence-bearing facts with exact path, artifact, receipt, or source references. "
    "Use observations for evidence and findings for unresolved problems."
)
EXECUTION_CONTROL_FINDINGS_DESCRIPTION = (
    "Unresolved problems with the execution-control assessment or permission proposal under review: "
    "unsupported classification, insufficient or mismatched evidence, invalid scope or permission "
    "reasoning, or contradictions/currentness that prevent endorsing this decision. "
    "If such a finding remains, use fail or blocked rather than pass."
)
EXECUTION_CONTROL_OBSERVATIONS_DESCRIPTION = (
    "Evidence-bearing facts with exact receipt or path references, including existing implementation, "
    "quality, or test defects. An existing product defect is not automatically a defect in this "
    "assessment; include it in findings only when explaining why it invalidates this assessment or "
    "requested authorization."
)


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "fail", "blocked"],
            "description": PASS_DESCRIPTION,
        },
        "rationale": {"type": "string"},
        "covered": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "description": GENERIC_FINDINGS_DESCRIPTION,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "statement": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["severity", "statement", "evidence"],
            },
        },
        "observations": {
            "type": "array",
            "description": GENERIC_OBSERVATIONS_DESCRIPTION,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"ref": {"type": "string"}, "detail": {"type": "string"}},
                "required": ["ref", "detail"],
            },
        },
        "dispositions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "resolution": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "resolution", "reason"],
            },
        },
    },
    "required": ["verdict", "rationale", "covered", "findings", "observations", "dispositions"],
}


def execution_control_resolution_instructions() -> str:
    """Return the exact vocabulary/mapping shared by execution-control prompts."""
    return (
        " Use exact execution-control disposition vocabulary only: "
        f"attempt:<epoch> uses {', '.join(ATTEMPT_RESOLUTIONS)}; "
        f"recovery:<proposal> and timeout:<proposal> use {', '.join(CONTROL_RESOLUTIONS)}. "
        "Do not use approve or any alias. Keep attempt classification independent "
        "from recovery and timeout decisions; the schema does not choose a "
        "substantive outcome for you."
    )


def review_output_contract(role: str) -> dict[str, str]:
    """Return the role-selected findings/observations contract.

    ``review_schema`` and the managed Runtime prompt both consume this helper;
    provider descriptions and reviewer instructions therefore describe the same
    evidence boundary without changing the JSON shape or validator behavior.
    """
    common = (
        " Use findings only for unresolved problems in the review subject and observations for "
        "evidence-bearing facts. A pass verdict requires findings=[]; if an unresolved finding "
        "remains, return fail or blocked. Do not automatically move findings or observations."
    )
    if role != "execution_control":
        return {
            "verdict_description": PASS_DESCRIPTION,
            "findings_description": GENERIC_FINDINGS_DESCRIPTION,
            "observations_description": GENERIC_OBSERVATIONS_DESCRIPTION,
            "instructions": common,
        }
    scope = (
        " For execution-control reviews, findings are unresolved problems with the assessment or "
        "permission proposal under review: unsupported classification, insufficient or mismatched "
        "evidence, invalid scope or permission reasoning, or contradictions/currentness that prevent "
        "endorsing this decision. Observations are evidence-bearing facts, including existing "
        "implementation, quality, or test defects with exact receipt/path references. An existing "
        "product defect is not automatically a defect in this assessment; include it in findings "
        "only when explaining why it invalidates this assessment or requested authorization. Do not "
        "mechanically move product defects out of findings or compel a progress conclusion."
    )
    return {
        "verdict_description": PASS_DESCRIPTION,
        "findings_description": EXECUTION_CONTROL_FINDINGS_DESCRIPTION,
        "observations_description": EXECUTION_CONTROL_OBSERVATIONS_DESCRIPTION,
        "instructions": common + scope + execution_control_resolution_instructions(),
    }


def review_output_instructions(role: str) -> str:
    """Return shared output-scope instructions for a managed review prompt."""
    return review_output_contract(role)["instructions"]


def review_schema(role: str) -> dict[str, Any]:
    """Return an independent provider/prompt schema for ``role``.

    The ordinary review contract remains byte-for-byte equivalent to the
    historical schema.  Execution-control providers receive one union enum;
    marker-specific validation remains the application's responsibility.
    """
    contract = review_output_contract(role)
    schema = deepcopy(REVIEW_SCHEMA)
    schema["properties"]["verdict"]["description"] = contract["verdict_description"]
    schema["properties"]["findings"]["description"] = contract["findings_description"]
    schema["properties"]["observations"]["description"] = contract["observations_description"]
    if role == "execution_control":
        resolution = schema["properties"]["dispositions"]["items"]["properties"]["resolution"]
        resolution["enum"] = list(EXECUTION_CONTROL_RESOLUTIONS)
        resolution["description"] = (
            "Use progress, no_progress, or inconclusive for attempt:<epoch>; "
            "use approved, rejected, or inconclusive for recovery:<proposal> and "
            "timeout:<proposal>. Do not use approve or aliases."
        )
    return schema


def validate_execution_control_dispositions(dispositions: Any) -> None:
    """Reject aliases while retaining the caller's raw result unchanged."""
    for item in dispositions:
        resolution = item.get("resolution") if isinstance(item, dict) else None
        need(
            resolution in EXECUTION_CONTROL_RESOLUTIONS,
            "invalid_review",
            "Execution-control disposition resolution must use exact typed vocabulary",
            {"resolution": resolution, "allowed": list(EXECUTION_CONTROL_RESOLUTIONS)},
        )
