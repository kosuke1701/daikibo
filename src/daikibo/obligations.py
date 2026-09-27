"""Requirement-qualified acceptance identity shared by planning and execution.

Legacy tasks retain plain review labels only when every label maps unambiguously
inside their declared input set. Explicit references use extra opaque, stable
markers, with their exact meaning included in the reviewer context.
"""
from __future__ import annotations

from .common import canonical, digest, need, obj, parse_json, strings, text


def marker(requirement: str, acceptance: str) -> str:
    return 'reqac:' + digest([requirement, acceptance])


def resolve(body: dict, requirements: dict[str, dict]) -> dict:
    labels = strings(body['acceptance'], 'acceptance', nonempty=True)
    need(len(labels) == len(set(labels)), 'duplicate_acceptance', 'Acceptance labels must be unique')
    candidates = {label: sorted(ident for ident, req in requirements.items()
                               if label in req.get('acceptance', [])) for label in labels}
    explicit = 'acceptance_refs' in body
    pairs = set()
    if explicit:
        refs = body['acceptance_refs']
        need(isinstance(refs, list) and len(refs) <= 10000,
             'invalid_acceptance_refs', 'acceptance_refs must be a bounded list')
        for ref in refs:
            obj(ref, required=('requirement', 'acceptance'), name='acceptance reference')
            ident = text(ref['requirement'], 'requirement ID', 200)
            ac = text(ref['acceptance'], 'acceptance ID', 4096)
            need(ident in requirements, 'invalid_acceptance_refs',
                 'Reference must name a requirement in read_artifacts', ident)
            need(ac in labels and ac in requirements[ident].get('acceptance', []),
                 'invalid_acceptance_refs', 'Acceptance is not declared by both task and requirement', ref)
            need((ident, ac) not in pairs, 'duplicate_acceptance_reference', 'Duplicate requirement/acceptance pair')
            pairs.add((ident, ac))
        mapped_labels = {ac for _, ac in pairs}
        need(mapped_labels == {label for label, ids in candidates.items() if ids},
             'invalid_acceptance_refs', 'Every requirement-related task label needs an explicit reference')
    else:
        ambiguous = {label: ids for label, ids in candidates.items() if len(ids) > 1}
        need(not ambiguous, 'ambiguous_acceptance',
             'Same acceptance label in multiple read requirements; revise the task with acceptance_refs', ambiguous)
        pairs = {(ids[0], label) for label, ids in candidates.items() if ids}
    refs = [{'requirement': ident, 'acceptance': ac, 'marker': marker(ident, ac)}
            for ident, ac in sorted(pairs)]
    coverage = list(labels) + ([ref['marker'] for ref in refs] if explicit else [])
    need(len(coverage) == len(set(coverage)), 'acceptance_marker_collision',
         'A display label collides with an exact acceptance marker')
    return {'pairs': pairs, 'references': refs, 'required_coverage': coverage,
            'explicit': explicit, 'supplemental_labels': [x for x in labels if not candidates[x]]}


def from_store(store, body: dict) -> dict:
    requirements = {}
    for ident in body['read_artifacts']:
        row = store.one('SELECT kind,body FROM artifacts WHERE id=?', (ident,), True)
        if row['kind'] == 'requirement':
            requirements[ident] = parse_json(row['body'])
    return resolve(body, requirements)


def review_task(store, body: dict) -> dict:
    """Construct a view; never rewrite the stored task just to add review markers."""
    result = from_store(store, body)
    view = {**body, 'acceptance': result['required_coverage']}
    if result['explicit']:
        view['acceptance_labels'] = list(body['acceptance'])
        view['acceptance_identity'] = result['references']
        view['acceptance_instruction'] = (
            'covered must include every acceptance label AND its requirement-qualified marker. '
            'Markers identify exact obligations, not evidence that they are satisfied. '
            'Independently judge each referenced requirement and its acceptance condition.'
        )
    return view


def delivery_coverage(requirements: list[dict]) -> dict:
    """Legacy unique labels remain usable; shared labels require exact pair markers.

    A label set alone loses identity at integrated review. Provide the complete
    mapping to the reviewer and use the same mapping in final certification.
    """
    labels = {}
    seen = set()
    for item in requirements:
        ident = item['id']
        need(ident not in seen, 'duplicate_requirement', 'Delivery requirement appears twice')
        seen.add(ident)
        for ac in item['body']['acceptance']:
            labels.setdefault(ac, set()).add(ident)
    required = sorted(labels)
    identities = []
    for label, ids in sorted(labels.items()):
        for ident in sorted(ids):
            exact = marker(ident, label)
            identities.append({'requirement':ident, 'acceptance':label, 'marker':exact, 'marker_required':len(ids)>1})
            if len(ids)>1: required.append(exact)
    need(len(set(required))==len(required), 'acceptance_marker_collision', 'Display label collides with a required exact marker')
    return {'required_coverage':required, 'acceptance_identity':identities,
            'acceptance_instruction':'Inspect every requirement/acceptance pair. A shared display label does not cover multiple requirements; include each required exact marker. Markers are identity, not proof of correctness.'}
