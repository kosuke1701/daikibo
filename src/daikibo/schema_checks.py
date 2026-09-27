"""Source-bound standard-schema diagnostics via the normal project API.

These reads are not execution/review receipts, waivers, or gate authorizations.
Actual contract-test evidence uses schema_cli through the existing Runtime.
"""
from __future__ import annotations

import re

from .common import digest, need
from .schema_evaluator import DIALECT, DEFAULT_LIMITS, Stopped, capabilities, check, exact_json, select


class SchemaChecks:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def capabilities(self, actor):
        actor.require('owner', 'agent', 'reviewer', 'observer', project=actor.project)
        return capabilities()

    def _document(self, actor, document, expected_digest):
        row = self.c.documents.get(actor, document)
        body = row['body']
        need(isinstance(expected_digest, str) and body['raw_digest'] == expected_digest,
             'stale_document', 'Provide the exact original document digest')
        raw = self.s.blob_get(expected_digest)
        need(digest(raw) == expected_digest, 'corrupt_document', 'Stored raw document changed')
        return row, raw

    @staticmethod
    def _dialect(raw, schema_pointer, dialect):
        """Never silently interpret OAS3.0 Schema Objects as JSON Schema 2020-12."""
        root = exact_json(raw)
        path, selected = select(root, schema_pointer)
        if isinstance(root, dict) and 'openapi' in root:
            version = root['openapi']
            need(isinstance(version, str) and re.fullmatch(r'3\.1\.\d+', version),
                 'unsupported_schema_dialect', 'Only selected OpenAPI 3.1 Schema Objects can use this profile')
            need(path != '#', 'schema_entry_required', 'An OpenAPI document is not a JSON Schema')
            # A non-default OAS dialect must not be overridden by a caller.
            default = root.get('jsonSchemaDialect', 'https://spec.openapis.org/oas/3.1/dialect/base')
            need(isinstance(default, str) and default in {DIALECT, 'https://spec.openapis.org/oas/3.1/dialect/base'},
                 'unsupported_schema_dialect', 'Custom OpenAPI schema dialect needs an appropriate validator')
            declared = selected.get('$schema') if isinstance(selected, dict) else None
            return dialect or declared or DIALECT
        declared = root.get('$schema') if isinstance(root, dict) else None
        chosen = dialect or (selected.get('$schema') if isinstance(selected, dict) else None) or declared
        # Applying a modern interpretation to draft07 (including $ref siblings)
        # is a semantic change, even if the caller asks for modern semantics.
        if declared is not None:
            need(declared == chosen, 'schema_dialect_mismatch', 'Do not override the source document dialect')
        return chosen

    def schema(self, actor, document, expected_digest, entry='#', dialect=None):
        row, raw = self._document(actor, document, expected_digest)
        chosen = dialect
        try:
            chosen = self._dialect(raw, entry, dialect)
        except Stopped:
            pass  # check() returns a typed, non-passing parse/selection diagnostic.
        result = check(raw, schema_pointer=entry, dialect=chosen)
        return self._result(result, {'document': document, 'raw_digest': expected_digest, 'entry': entry}, None)

    def instance(self, actor, document, expected_digest, instance_document, instance_digest,
                 entry='#', instance_entry='#', dialect=None):
        schema_row, raw = self._document(actor, document, expected_digest)
        data_row, data = self._document(actor, instance_document, instance_digest)
        need(schema_row['project'] == data_row['project'], 'cross_project', 'Schema and instance must belong to the same project')
        chosen = dialect
        try:
            chosen = self._dialect(raw, entry, dialect)
        except Stopped:
            pass
        result = check(raw, data, schema_pointer=entry, instance_pointer=instance_entry, dialect=chosen)
        return self._result(result, {'document': document, 'raw_digest': expected_digest, 'entry': entry},
                            {'document': instance_document, 'raw_digest': instance_digest, 'entry': instance_entry})

    @staticmethod
    def _result(result, schema, instance):
        from . import __version__
        report = {'format': 'daikibo.schema-diagnostic.v1', 'implementation_version': __version__,
                  'schema': schema, 'instance': instance, 'result': result,
                  'test_receipt_created': False, 'review_performed': False,
                  'adopted': False, 'deploy_ready': False}
        report['digest'] = digest(report)
        return report
