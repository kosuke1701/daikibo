"""Deterministic protocol fixture for assurance gate tests only."""

import json
import sys


request = json.load(sys.stdin)
context = request.get("context", {})
print(json.dumps({
    "verdict": "pass",
    "rationale": "Deterministic assurance protocol fixture; not semantic acceptance.",
    "covered": context.get("required_coverage", []),
    "findings": [],
    "observations": [{"ref": request.get("subject"), "detail": "Fixture read the packet markers."}],
    "dispositions": [],
}))
