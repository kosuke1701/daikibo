"""Locally authored tests, not the official JSON Schema conformance suite."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from daikibo.common import canonical
from daikibo.schema_evaluator import DIALECT, DEFAULT_LIMITS, Schema, Stopped, capabilities, check, exact_json


def evaluate(schema, data, **kwargs):
    if isinstance(schema, dict):
        schema = {'$schema': DIALECT, **schema}
    return check(canonical(schema), data if isinstance(data, bytes) else canonical(data), dialect=DIALECT, **kwargs)


@pytest.mark.parametrize('schema,data,expected', [
    (True, None, True), (False, 'x', False), ({}, [], True),
    ({'type': 'integer'}, b'1.0', True), ({'type': 'integer'}, b'1.1', False),
    ({'type': 'integer'}, True, False), ({'type': 'number'}, False, False),
    ({'type': ['null', 'string']}, None, True), ({'type': ['null', 'string']}, 3, False),
    ({'type': 'array'}, {}, False), ({'type': 'object'}, [], False),
    ({'minimum': 5}, 'not-a-number', True), ({'minLength': 2}, 1, True),
    ({'required': ['x']}, 2, True), ({'minItems': 2}, None, True),
    ({'minimum': 5}, 5, True), ({'minimum': 5}, 4, False),
    ({'maximum': 5}, 5, True), ({'maximum': 5}, 6, False),
    ({'exclusiveMinimum': 5}, 5, False), ({'exclusiveMaximum': 5}, 5, False),
    ({'minimum': 5, 'maximum': 4}, 4, False),  # Valid but unsatisfiable Schema.
    ({'minLength': 2, 'maxLength': 2}, '日😀', True),
    ({'maxLength': 1}, '\x00', True),
    ({'minItems': 1, 'maxItems': 2}, [1, 2, 3], False),
    ({'minProperties': 1, 'maxProperties': 2}, {'a': 1}, True),
    ({'required': [''], 'additionalProperties': True}, {'': 0}, True),
    ({'required': ['a']}, {}, False),
    ({'properties': {'a': {'type': 'integer'}}}, {}, True),
    ({'properties': {'a': {'type': 'integer'}}, 'additionalProperties': False}, {'a': 1, 'b': 2}, False),
    ({'properties': {'a': True}, 'additionalProperties': {'type': 'number'}}, {'a': 'anything', 'b': 'bad'}, False),
    ({'properties': {'a': True}, 'additionalProperties': {'type': 'number'}}, {'a': 'anything', 'b': 1}, True),
    ({'propertyNames': {'minLength': 2}}, {'a': 1}, False),
    ({'propertyNames': False}, {}, True),
    ({'dependentRequired': {'a': ['b']}}, {'a': 1}, False),
    ({'dependentRequired': {'a': ['b']}}, {'b': 1}, True),
    ({'dependentSchemas': {'a': {'required': ['b']}}}, {'a': 1}, False),
    ({'dependentSchemas': {'a': False}}, {'b': 1}, True),
    ({'prefixItems': [{'type': 'integer'}, {'type': 'string'}], 'items': False}, [1, 'a'], True),
    ({'prefixItems': [True], 'items': False}, [1, 2], False),
    ({'items': {'type': 'integer'}}, [1, False], False),
    ({'items': False}, [], True),
    ({'contains': {'type': 'integer'}}, [], False),
    ({'contains': {'type': 'integer'}, 'minContains': 0}, [], True),
    ({'contains': {'type': 'integer'}, 'minContains': 2, 'maxContains': 2}, [1, 'x', 2], True),
    ({'contains': {'type': 'integer'}, 'maxContains': 1}, [1, 2], False),
    ({'minContains': 3, 'maxContains': 1}, [], True),  # No contains: no assertion.
    ({'uniqueItems': True}, [1, True], True),
    ({'uniqueItems': True}, b'[1, 1.0]', False),
    ({'uniqueItems': True}, [{'a': 1, 'b': 2}, {'b': 2, 'a': 1}], False),
    ({'const': 1}, b'1.0', True), ({'const': 1}, True, False),
    ({'const': {'a': [1]}}, b'{"a":[1.0]}', True),
    ({'enum': []}, 1, False), ({'enum': [True, False]}, 0, False),
    ({'enum': [1, 1]}, 1, True),  # Duplicate enum entries SHOULD, not MUST, be unique.
    ({'allOf': [{'minimum': 1}, {'maximum': 5}]}, 4, True),
    ({'allOf': [{'minimum': 1}, {'maximum': 5}]}, 7, False),
    ({'anyOf': [{'type': 'string'}, {'type': 'number'}]}, 3, True),
    ({'anyOf': [{'type': 'string'}, {'type': 'number'}]}, None, False),
    ({'oneOf': [{'type': 'integer'}, {'type': 'number'}]}, 1, False),
    ({'not': {'type': 'string'}}, 5, True),
    ({'not': {}}, 5, False),
    ({'if': {'type': 'integer'}, 'then': {'minimum': 2}, 'else': {'type': 'string'}}, 1, False),
    ({'if': {'type': 'integer'}, 'then': {'minimum': 2}, 'else': {'type': 'string'}}, 'x', True),
    ({'then': False, 'else': False}, 1, True),
    ({'if': False}, 1, True),
    ({'allOf': [{'properties': {'a': True}}], 'additionalProperties': False}, {'a': 1}, False),
])
def test_assertion_and_applicator_semantics(schema, data, expected):
    result = evaluate(schema, data)
    assert result['valid'] is expected, result
    assert result['status'] == ('valid' if expected else 'invalid')
    assert result['complete_standard_conformance'] is False
    assert result['compatibility_proven'] is False
    if expected:
        assert result['errors'] == []


@pytest.mark.parametrize('schema,data,expected', [
    (b'{"multipleOf":0.01}', b'4.02', True),
    (b'{"multipleOf":0.01}', b'4.020000000000000000000000000001', False),
    (b'{"multipleOf":0.000000000000000000000000000001}', b'1', True),
    (b'{"maximum":0.100000000000000000000000000001}', b'0.100000000000000000000000000002', False),
    (b'{"const":9007199254740993}', b'9007199254740992', False),
    (b'{"const":1e1000}', b'1e1000', True),
    (b'{"multipleOf":3}', b'-9', True),
    (b'{"type":"integer"}', b'0e-2000', True),
])
def test_exact_arithmetic_no_binary_rounding(schema, data, expected):
    assert check(schema, data, dialect=DIALECT)['valid'] is expected


def test_reference_siblings_and_recursive_data():
    schema = {'$defs': {'amount': {'type': 'number'}}, '$ref': '#/$defs/amount', 'minimum': 2}
    assert evaluate(schema, 1)['valid'] is False
    assert evaluate(schema, 2)['valid'] is True
    recursive = {'type': 'object', 'properties': {'next': {'$ref': '#'}}}
    assert evaluate(recursive, {'next': {'next': {}}})['valid'] is True
    assert evaluate(recursive, {'next': {'next': 1}})['valid'] is False


def test_local_anchor_and_escaped_pointer():
    schema = {'$defs': {'a/~日': {'$anchor': 'A', 'type': 'integer'}}, '$ref': '#A'}
    assert evaluate(schema, 1)['valid'] is True
    schema['$ref'] = '#/$defs/a~1~0%E6%97%A5'
    assert evaluate(schema, False)['valid'] is False


def test_root_id_reference_and_empty_self_reference():
    value = {'$id': 'https://example.org/a.json', '$defs': {'n': {'type': 'number'}}, '$ref': 'https://example.org/a.json#/$defs/n'}
    assert evaluate(value, 3)['valid'] is True
    for ref in ('#', ''):
        result = evaluate({'$ref': ref}, 3)
        assert result['valid'] is None and result['status'] == 'unsupported'
        assert result['reason']['code'] == 'non_progressing_reference'


@pytest.mark.parametrize('schema', [
    {'pattern': 'a+'}, {'patternProperties': {'a': {}}}, {'format': 'email'},
    {'unevaluatedProperties': False}, {'$dynamicRef': '#node'}, {'customRule': False},
    {'$defs': {'unused': {'pattern': 'x'}}}, {'anyOf': [True, {'format': 'email'}]},
    {'$ref': 'https://example.org/remote.json'}, {'$ref': '#missing'},
    {'$defs': {'child': {'$id': 'nested.json', 'type': 'number'}}},
    {'dependencies': {'x': ['y']}}, {'contentEncoding': 'base64'},
])
def test_unsupported_never_becomes_pass_or_negative_example(schema):
    result = evaluate(schema, None)
    assert result['status'] == 'unsupported' and result['valid'] is None, result
    assert result['instance_evaluated'] is False


@pytest.mark.parametrize('schema', [
    {'type': ['number', 'number']}, {'type': []}, {'type': ['new-type']}, {'type': 1},
    {'enum': 1}, {'minimum': True}, {'multipleOf': 0}, {'multipleOf': -1},
    {'minItems': -1}, {'maxLength': '2'}, {'required': ['a', 'a']},
    {'required': 'x'}, {'required': [{}]}, {'dependentRequired': {'x': [1]}},
    {'properties': []}, {'items': []}, {'prefixItems': []}, {'allOf': []},
    {'not': 42}, {'$defs': {'x': 1}}, {'uniqueItems': 1},
    {'examples': {}}, {'readOnly': 'yes'}, {'description': 2},
    {'$ref': 1}, {'$id': 'https://example.org/x#fragment'}, {'$id': 'http://[bad'},
    {'$defs': {'a': {'$anchor': 'X'}, 'b': {'$anchor': 'X'}}},
    {'$ref': '#/missing'}, {'$ref': '#/bad~3escape'},
])
def test_invalid_schema_is_not_invalid_instance(schema):
    result = evaluate(schema, None)
    assert result['status'] == 'invalid_schema' and result['valid'] is None, result


def test_annotations_are_not_schemas_or_coercion():
    schema = {'default': {'unknownKeyword': 'retained'}, 'examples': [{'pattern': '+'}],
              'title': 'x', 'readOnly': True, 'deprecated': True,
              'properties': {'default': {'const': {'pattern': 'literal'}}}}
    result = evaluate(schema, {'default': {'pattern': 'literal'}})
    assert result['valid'] is True and result['annotation_count'] == 5
    assert evaluate({'default': 5, 'required': ['a']}, {})['valid'] is False


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'NaN', b'Infinity', b'-Infinity', b'\xff', b'"\\ud800"'])
def test_invalid_raw_json_is_not_an_invalid_example(raw):
    result = check(b'{}', raw, dialect=DIALECT)
    assert result['status'] == 'invalid_json' and result['valid'] is None


def test_explicit_dialect_required_and_wrong_draft_cannot_pass():
    for schema, dialect in [(b'{}', None), (b'{"$schema":"https://example.org/custom"}', DIALECT),
                            (b'{"$schema":"http://json-schema.org/draft-07/schema#"}', None)]:
        result = check(schema, b'1', dialect=dialect)
        assert result['status'] == 'unsupported' and result['valid'] is None


@pytest.mark.parametrize('limits,schema,data', [
    (replace(DEFAULT_LIMITS, max_bytes=2), b'{}', b'123'),
    (replace(DEFAULT_LIMITS, max_json_nodes=3), b'{}', b'[1,2,3]'),
    (replace(DEFAULT_LIMITS, max_depth=2), b'{}', b'[[[1]]]'),
    (replace(DEFAULT_LIMITS, max_schema_nodes=1), b'{"items":{}}', b'[]'),
    (replace(DEFAULT_LIMITS, max_steps=2), b'{"items":{}}', b'[1,2,3]'),
    (DEFAULT_LIMITS, b'{}', b'1e9999'),
    (replace(DEFAULT_LIMITS, max_number_digits=2), b'{}', b'123'),
])
def test_capacity_is_explicit_and_cannot_match_expected_invalid(limits, schema, data):
    result = check(schema, data, dialect=DIALECT, limits=limits)
    assert result['status'] == 'limit_exceeded' and result['valid'] is None, result


def test_complete_diagnostic_status_survives_issue_display_limit():
    schema = {**{'x'+str(i): {} for i in range(6)}, 'minimum': True}
    result = evaluate(schema, 1, limits=replace(DEFAULT_LIMITS, max_issues=2))
    assert result['schema_issue_count'] == 7 and result['schema_issues_truncated']
    assert len(result['schema_issues']) == 2 and result['status'] == 'invalid_schema'


def test_embedded_resource_id_is_not_resolved_against_enclosing_document():
    raw = canonical({'$schema': DIALECT, '$defs': {'x': {'$id': 'nested.json', '$ref': '#'}}})
    result = check(raw, b'1', schema_pointer='#/$defs/x', dialect=DIALECT)
    assert result['status'] == 'unsupported' and result['valid'] is None


def test_raw_data_remains_immutable_and_instance_selection_is_exact():
    root = {'$schema': DIALECT, '$defs': {'node': {'const': 1}}}
    result = check(canonical(root), b'{"a/b":[0,1.0]}', schema_pointer='#/$defs/node',
                   instance_pointer='#/a~1b/1', dialect=DIALECT)
    assert result['valid'] is True
    assert root == {'$schema': DIALECT, '$defs': {'node': {'const': 1}}}


def test_capabilities_are_explicit_and_python_floats_not_accepted():
    cap = capabilities()
    assert not cap['complete_standard_conformance'] and 'format' in cap['not_implemented']
    with pytest.raises(Stopped):
        Schema({'minimum': 0.1}, dialect=DIALECT)


def test_direct_cli_engine_cannot_override_parent_dialect_or_legacy_oas():
    for root in ({'$schema': 'http://json-schema.org/draft-07/schema#', '$defs': {'x': {'type': 'number'}}},
                 {'openapi': '3.0.4', '$defs': {'x': {'type': 'number'}}},
                 {'openapi': '3.1.1', 'jsonSchemaDialect': 'https://example.org/custom', '$defs': {'x': {'type': 'number'}}}):
        result = check(canonical(root), b'1', schema_pointer='#/$defs/x', dialect=DIALECT)
        assert result['valid'] is None and result['status'] == 'unsupported'


def test_pointer_bad_percent_and_long_index_do_not_escape_as_unhandled_errors():
    for pointer in ('#/x/%E6', '#/x/%XX', '#/x/' + '9' * 5000):
        result = check(b'{"$schema":"https://json-schema.org/draft/2020-12/schema"}',
                       b'{"x":[]}', instance_pointer=pointer)
        assert result['valid'] is None


def test_pure_false_schema_does_not_ignore_invalid_instance_bytes():
    result = check(b'false', b'broken JSON', dialect=DIALECT)
    assert result['valid'] is None and result['status'] == 'invalid_json'


def test_long_schema_location_stops_before_producing_unbounded_diagnostic():
    result = evaluate({'properties': {'x'*17000: False}}, {})
    assert result['status'] == 'limit_exceeded' and result['valid'] is None
    assert len(canonical(result)) < 5000


def test_missing_instance_selection_is_not_labeled_a_broken_schema():
    r = check(b'{}', b'{"value": 1}', dialect=DIALECT, instance_pointer='#/other')
    assert r['status'] == 'invalid_instance_selection' and r['valid'] is None


@pytest.mark.parametrize('token', [b'1e99999999999999999999999999', b'1e-99999999999999999999999999'])
@pytest.mark.parametrize('as_schema', [False, True])
def test_decimal_runtime_exponent_overflow_is_nonpassing(token, as_schema):
    raw = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"number"}'
    if as_schema:
        result = check(raw[:-1] + b',"minimum":' + token + b'}', b'1')
    else:
        result = check(raw, token)
    assert result['status'] == 'limit_exceeded'
    assert result['valid'] is None
    assert result['reason']['code'] == 'number_capacity'
