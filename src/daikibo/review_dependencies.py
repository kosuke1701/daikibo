"""Canonical accepted constraints shared by review inputs and adoption checks."""
from .common import parse_json


def accepted_invariants(store, project):
    result = []
    for row in store.all(
        "SELECT id,revision,digest,body FROM artifacts "
        "WHERE project=? AND status='accepted' ORDER BY id", (project,)
    ):
        body = parse_json(row['body'])
        if body.get('constraints') or body.get('critical'):
            result.append({'id': row['id'], 'revision': row['revision'],
                           'digest': row['digest'], 'statement': body['statement'],
                           'constraints': body.get('constraints', {})})
    return result
