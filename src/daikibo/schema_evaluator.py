"""Exact, bounded JSON Schema 2020-12 instance diagnostics.

Not a complete standards validator. Unsupported schema features are never ignored
or treated as valid. No code generation, network resolution, mutation, receipt
creation, adoption or engineering-gate decisions occur in this module.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

DIALECT = 'https://json-schema.org/draft/2020-12/schema'
ENGINE = 'daikibo.json-schema-static.v1'
TYPES = {'null', 'boolean', 'integer', 'number', 'string', 'array', 'object'}
ANNOTATIONS = {'title', 'description', '$comment', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly'}
NUMERIC = {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'}
COUNTS = {'minLength', 'maxLength', 'minItems', 'maxItems', 'minProperties', 'maxProperties', 'minContains', 'maxContains'}
SCHEMA_MAPS = {'properties', '$defs', 'dependentSchemas'}
SCHEMA_ARRAYS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
SCHEMA_SINGLE = {'additionalProperties', 'propertyNames', 'items', 'contains', 'not', 'if', 'then', 'else'}
KEYWORDS = (ANNOTATIONS | NUMERIC | COUNTS | SCHEMA_MAPS | SCHEMA_ARRAYS | SCHEMA_SINGLE |
            {'$schema', '$id', '$ref', '$anchor', 'type', 'enum', 'const', 'required', 'uniqueItems', 'dependentRequired'})


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 6_000_000
    max_json_nodes: int = 200_000
    max_schema_nodes: int = 20_000
    max_depth: int = 64
    max_steps: int = 200_000
    max_issues: int = 100
    max_number_digits: int = 1024
    max_number_exponent: int = 2048
    max_pointer_characters: int = 16_000


DEFAULT_LIMITS = Limits()


class Stopped(Exception):
    def __init__(self, status: str, code: str, message: str, path: str = '#'):
        super().__init__(message)
        self.status, self.code, self.path = status, code, path


def escape(value: str | int) -> str:
    return str(value).replace('~', '~0').replace('/', '~1')


def number(value: Any) -> bool:
    return type(value) in (int, Decimal)


def integral(value: Any) -> bool:
    return type(value) is int or (type(value) is Decimal and value == value.to_integral_value())


def check_tree(value: Any, limits: Limits) -> None:
    """Bound data before recursion/hashing; don't accept Python-only values."""
    stack = [(value, 0)]
    seen = 0
    while stack:
        node, depth = stack.pop()
        seen += 1
        if depth > limits.max_depth or seen > limits.max_json_nodes:
            raise Stopped('limit_exceeded', 'json_capacity', 'JSON node/depth limit exceeded')
        if type(node) is dict:
            if not all(type(k) is str for k in node):
                raise Stopped('invalid_json', 'non_string_key', 'Object keys must be strings')
            stack.extend((v, depth + 1) for v in node.values())
            stack.extend((k, depth + 1) for k in node)
        elif type(node) is list:
            stack.extend((v, depth + 1) for v in node)
        elif type(node) is str:
            try:
                node.encode('utf-8')
            except UnicodeError as exc:
                raise Stopped('invalid_json', 'unpaired_surrogate', 'Unpaired Unicode surrogate is not supported') from exc
        elif type(node) is Decimal:
            digits = node.as_tuple()
            if not node.is_finite():
                raise Stopped('invalid_json', 'nonfinite_number', 'JSON numbers must be finite')
            if len(digits.digits) > limits.max_number_digits or abs(digits.exponent) > limits.max_number_exponent:
                raise Stopped('limit_exceeded', 'number_capacity', 'Number digit/exponent capacity exceeded')
        elif type(node) is int:
            if node.bit_length() > limits.max_number_digits * 4:
                raise Stopped('limit_exceeded', 'number_capacity', 'Integer capacity exceeded')
        elif node is not None and type(node) is not bool:
            raise Stopped('invalid_json', 'non_json_value', 'Use exact JSON bytes, not floats or arbitrary Python objects')


def exact_json(raw: bytes, limits: Limits = DEFAULT_LIMITS) -> Any:
    if type(raw) is not bytes or len(raw) > limits.max_bytes:
        raise Stopped('limit_exceeded', 'byte_capacity', 'JSON byte limit exceeded')

    def decimal(token):
        if len(token) > limits.max_number_digits + 20:
            raise Stopped('limit_exceeded', 'number_capacity', 'Numeric token too long')
        # Decimal has implementation exponent limits before check_tree can run.
        # Refuse capacity overflow explicitly instead of leaking InvalidOperation.
        exponent = token.lower().partition('e')[2]
        if exponent and abs(int(exponent)) > limits.max_number_exponent:
            raise Stopped('limit_exceeded', 'number_capacity', 'Numeric exponent capacity exceeded')
        try:
            return Decimal(token)
        except InvalidOperation as exc:
            raise Stopped('limit_exceeded', 'number_capacity', 'Decimal representation capacity exceeded') from exc

    def integer(token):
        if len(token.lstrip('-')) > limits.max_number_digits:
            raise Stopped('limit_exceeded', 'number_capacity', 'Integer token too long')
        return int(token)

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise Stopped('invalid_json', 'duplicate_key', 'Duplicate JSON property')
            value[key] = item
        return value

    def constant(token):
        raise Stopped('invalid_json', 'nonfinite_number', 'Non-JSON numeric constant')

    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                           parse_float=decimal, parse_int=integer, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Stopped('invalid_json', 'parse_error', 'Invalid or excessively nested UTF-8 JSON') from exc
    check_tree(value, limits)
    return value


def select(root: Any, fragment: str) -> tuple[str, Any]:
    """Normalize a percent-encoded RFC6901 fragment, without guessing array indices."""
    if not isinstance(fragment, str) or not fragment.startswith('#'):
        raise Stopped('invalid_schema', 'invalid_pointer', 'Use a local JSON Pointer fragment')
    if len(fragment) > 16_000:
        raise Stopped('limit_exceeded', 'pointer_capacity', 'Pointer exceeds capacity')
    if re.search(r'%(?![0-9a-fA-F]{2})', fragment):
        raise Stopped('invalid_schema', 'invalid_pointer', 'Malformed percent escape')
    try:
        path = urllib.parse.unquote(fragment[1:], errors='strict')
    except UnicodeError as exc:
        raise Stopped('invalid_schema', 'invalid_pointer', 'Invalid UTF-8 pointer') from exc
    if not path:
        return '#', root
    if not path.startswith('/'):
        raise Stopped('unsupported', 'anchor_selection', 'Select the Schema entry by a JSON Pointer')
    node, parts = root, []
    for token in path[1:].split('/'):
        if re.search(r'~(?![01])', token):
            raise Stopped('invalid_schema', 'invalid_pointer', 'Malformed JSON Pointer escape')
        key = token.replace('~1', '/').replace('~0', '~')
        parts.append(escape(key))
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and re.fullmatch(r'0|[1-9][0-9]*', key) and len(key) <= 10 and int(key) < len(node):
            node = node[int(key)]
        else:
            raise Stopped('invalid_schema', 'missing_pointer', 'JSON Pointer target is absent', fragment)
    return '#/' + '/'.join(parts), node


def identity(value: Any) -> Any:
    """JSON equality: true != 1, 1 == 1.0, object order is irrelevant."""
    if number(value):
        return ('number', value)
    if isinstance(value, list):
        return ('array', tuple(identity(v) for v in value))
    if isinstance(value, dict):
        return ('object', tuple(sorted((k, identity(v)) for k, v in value.items())))
    return (type(value).__name__, value)


def capabilities() -> dict:
    from dataclasses import asdict
    return {'engine': ENGINE, 'dialects': [DIALECT], 'keywords': sorted(KEYWORDS),
            'limits': asdict(DEFAULT_LIMITS), 'local_static_refs': True,
            'external_ref_fetch': False, 'nested_id_resources': False,
            'unknown_keyword_policy': 'unsupported_before_any_instance_pass',
            'annotations_only': sorted(ANNOTATIONS),
            'not_implemented': ['pattern', 'patternProperties', 'format', 'content*', '$dynamicRef',
                                '$dynamicAnchor', '$vocabulary', 'unevaluated*', 'other dialects',
                                'nested $id', 'external URI reference resolution'],
            'complete_standard_conformance': False, 'compatibility_proven': False,
            'semantic_review_required': True}


class Schema:
    """Compile supported Schema positions only; never interpret default/const as schemas."""
    def __init__(self, root: Any, entry: str = '#', dialect: str | None = None,
                 limits: Limits = DEFAULT_LIMITS):
        self.root, self.limits = root, limits
        self.entry, selected = select(root, entry)
        check_tree(root, limits)
        declared = selected.get('$schema') if isinstance(selected, dict) else None
        root_dialect = root.get('$schema') if isinstance(root, dict) else None
        if root_dialect is not None and root_dialect != DIALECT:
            raise Stopped('unsupported', 'dialect', 'Root document declares a different dialect')
        if isinstance(root, dict) and 'openapi' in root:
            version = root['openapi']
            if not isinstance(version, str) or not re.fullmatch(r'3\.1\.\d+', version) or self.entry == '#':
                raise Stopped('unsupported', 'openapi_schema_context', 'Select a Schema within an OpenAPI 3.1 document')
            oas_dialect = root.get('jsonSchemaDialect', 'https://spec.openapis.org/oas/3.1/dialect/base')
            if not isinstance(oas_dialect, str) or oas_dialect not in {DIALECT, 'https://spec.openapis.org/oas/3.1/dialect/base'}:
                raise Stopped('unsupported', 'dialect', 'Unknown OpenAPI Schema dialect')
        chosen = dialect or declared or root_dialect
        if chosen != DIALECT or (declared is not None and declared != chosen):
            raise Stopped('unsupported', 'dialect', 'An exact, explicit 2020-12 dialect is required', self.entry)
        self.dialect = chosen
        self.nodes: dict[str, Any] = {}
        self.refs: dict[str, str] = {}
        self.anchors: dict[str, str] = {}
        self.issues: list[dict] = []
        self.issue_count = 0
        self.issue_kinds = set()
        self.annotation_locations: list[dict] = []
        self.annotation_count = 0
        self.base_id = selected.get('$id', '') if isinstance(selected, dict) else ''
        if not isinstance(self.base_id, str):
            self.base_id = ''  # Compiler records invalid $id below.
        # A fragment under an embedded resource has a different reference base.
        # Refuse that case rather than resolve against the enclosing document.
        if self.entry != '#':
            cursor = '#'
            for token in self.entry[2:].split('/'):
                cursor += '/' + token
                _, ancestor = select(root, cursor)
                if isinstance(ancestor, dict) and '$schema' in ancestor and ancestor['$schema'] != DIALECT:
                    raise Stopped('unsupported', 'dialect', 'An ancestor declares a different dialect', cursor)
                if isinstance(ancestor, dict) and '$id' in ancestor:
                    raise Stopped('unsupported', 'embedded_base_uri', 'Embedded resource base is not supported', cursor)
        self._scan([(self.entry, selected, 0)])
        # References may introduce additional schema roots (e.g. OAS components).
        scanned_refs = set()
        while True:
            pending = [(path, node['$ref']) for path, node in self.nodes.items()
                       if isinstance(node, dict) and '$ref' in node and path not in scanned_refs]
            if not pending:
                break
            for path, ref in pending:
                scanned_refs.add(path)
                try:
                    target_path, target = self._resolve(ref)
                    self.refs[path] = target_path
                    if target_path not in self.nodes:
                        self._scan([(target_path, target, 0)])
                except Stopped as exc:
                    self._issue(exc.status, path + '/$ref', exc.code, str(exc))
        # Even with a boolean/anyOf branch that could succeed, don't ignore an
        # unsupported or invalid sibling/definition and pronounce the contract valid.
        kinds = self.issue_kinds
        self.status = 'invalid_schema' if 'invalid_schema' in kinds else 'unsupported' if kinds else 'supported'

    def _issue(self, status, path, code, message):
        if len(path) > self.limits.max_pointer_characters:
            raise Stopped('limit_exceeded', 'pointer_capacity', 'Diagnostic location exceeds capacity')
        self.issue_count += 1
        self.issue_kinds.add(status)
        if len(self.issues) < self.limits.max_issues:
            self.issues.append({'status': status, 'schema_pointer': path, 'code': code, 'message': message})

    def _check(self, condition, path, code, message):
        if not condition:
            self._issue('invalid_schema', path, code, message)
        return bool(condition)

    def _scan(self, pending):
        while pending:
            path, node, depth = pending.pop()
            if len(path) > self.limits.max_pointer_characters:
                raise Stopped('limit_exceeded', 'pointer_capacity', 'Schema location exceeds capacity')
            if path in self.nodes:
                continue
            if len(self.nodes) >= self.limits.max_schema_nodes or depth > self.limits.max_depth:
                raise Stopped('limit_exceeded', 'schema_capacity', 'Schema node/depth capacity exceeded', path)
            self.nodes[path] = node
            if type(node) is bool:
                continue
            if not self._check(type(node) is dict, path, 'schema_type', 'A Schema must be an object or boolean'):
                continue
            for key, value in node.items():
                where = path + '/' + escape(key)
                if key not in KEYWORDS:
                    self._issue('unsupported', where, 'keyword', 'Keyword is not implemented by this profile')
                    continue
                if key in ANNOTATIONS:
                    self.annotation_count += 1
                    if len(self.annotation_locations) < self.limits.max_issues:
                        self.annotation_locations.append({'schema_pointer': where, 'keyword': key})
                    if key in {'title', 'description', '$comment'}:
                        self._check(type(value) is str, where, 'annotation_type', 'Text annotation must be a string')
                    elif key in {'readOnly', 'writeOnly', 'deprecated'}:
                        self._check(type(value) is bool, where, 'annotation_type', 'Flag annotation must be boolean')
                    elif key == 'examples':
                        self._check(type(value) is list, where, 'annotation_type', 'examples must be an array')
                elif key == '$schema':
                    if value != self.dialect:
                        self._issue('unsupported', where, 'dialect', 'Nested/unknown dialect is not supported')
                elif key == '$id':
                    if self._check(type(value) is str and not re.search(r'\s', value), where, 'id_type', '$id must be a URI string'):
                        try:
                            if urllib.parse.urlsplit(value).fragment:
                                self._issue('invalid_schema', where, 'id_fragment', '$id must not contain a nonempty fragment')
                        except ValueError:
                            self._issue('invalid_schema', where, 'invalid_uri', 'Invalid base URI')
                        if path != self.entry:
                            self._issue('unsupported', where, 'nested_base_uri', 'Nested resource bases require a different resolver')
                elif key == '$anchor':
                    if self._check(type(value) is str and re.fullmatch(r'[A-Za-z_][-A-Za-z0-9._]*', value), where, 'anchor_name', 'Invalid anchor'):
                        if value in self.anchors and self.anchors[value] != path:
                            self._issue('invalid_schema', where, 'duplicate_anchor', 'Duplicate anchor')
                        self.anchors[value] = path
                elif key == '$ref':
                    self._check(type(value) is str, where, 'ref_type', '$ref must be a URI-reference string')
                elif key == 'type':
                    values = [value] if type(value) is str else value
                    self._check(type(values) is list and bool(values) and all(type(x) is str and x in TYPES for x in values)
                                and len(set(values)) == len(values), where, 'type', 'Invalid type or type union')
                elif key == 'enum':
                    self._check(type(value) is list, where, 'enum_type', 'enum must be an array')
                elif key in NUMERIC:
                    self._check(number(value) and (key != 'multipleOf' or value > 0), where, 'numeric_rule', 'Numeric bound required; multipleOf must be positive')
                elif key in COUNTS:
                    self._check(integral(value) and value >= 0, where, 'count_rule', 'Nonnegative integer bound required')
                elif key == 'uniqueItems':
                    self._check(type(value) is bool, where, 'boolean_rule', 'uniqueItems must be boolean')
                elif key == 'required':
                    self._string_set(value, where)
                elif key == 'dependentRequired':
                    if self._check(type(value) is dict, where, 'map_rule', 'dependentRequired must be an object'):
                        for name, vals in value.items():
                            self._string_set(vals, where + '/' + escape(name))
                elif key in SCHEMA_MAPS:
                    if self._check(type(value) is dict, where, 'schema_map', 'Expected a map of schemas'):
                        pending.extend((where + '/' + escape(name), child, depth + 1) for name, child in value.items())
                elif key in SCHEMA_ARRAYS:
                    if self._check(type(value) is list and bool(value), where, 'schema_array', 'Expected a nonempty array of schemas'):
                        pending.extend((where + '/' + str(i), child, depth + 1) for i, child in enumerate(value))
                elif key in SCHEMA_SINGLE:
                    pending.append((where, value, depth + 1))
                # const intentionally accepts any JSON value and is not traversed.

    def _string_set(self, value, path):
        self._check(type(value) is list and all(type(x) is str for x in value) and len(value) == len(set(value)),
                    path, 'string_set', 'Expected a unique string array')

    def _resolve(self, ref):
        if not isinstance(ref, str):
            raise Stopped('invalid_schema', 'ref_type', 'Reference must be a string')
        if ref == '':
            ref = '#'
        if self.base_id and not ref.startswith('#'):
            try:
                absolute = urllib.parse.urljoin(self.base_id, ref)
                address, fragment = urllib.parse.urldefrag(absolute)
            except ValueError as exc:
                raise Stopped('invalid_schema', 'invalid_uri', 'Invalid reference URI') from exc
            if address == urllib.parse.urldefrag(self.base_id)[0]:
                ref = '#' + fragment
        if not ref.startswith('#'):
            raise Stopped('unsupported', 'external_reference', 'Only registered local references are evaluated; no network is fetched')
        if ref == '#' or ref.startswith('#/') or ref.startswith('#%2F') or ref.startswith('#%2f'):
            return select(self.root, ref)
        name = urllib.parse.unquote(ref[1:])
        if name not in self.anchors:
            raise Stopped('unsupported', 'unresolved_anchor', 'Static anchor is missing')
        target = self.anchors[name]
        return target, self.nodes[target]

    def report(self):
        return {'engine': ENGINE, 'dialect': self.dialect, 'entry': self.entry, 'schema_status': self.status,
                'schema_nodes': len(self.nodes), 'limits': asdict(self.limits), 'schema_issues': self.issues,
                'schema_issue_count': self.issue_count, 'schema_issues_truncated': self.issue_count > len(self.issues),
                'annotation_locations': self.annotation_locations, 'annotation_count': self.annotation_count,
                'complete_standard_conformance': False, 'compatibility_proven': False,
                'semantic_review_required': True}

    def evaluate(self, instance):
        report = self.report()
        if self.status != 'supported':
            return {**report, 'status': self.status, 'valid': None, 'evaluation_steps': 0, 'errors': []}
        check_tree(instance, self.limits)
        runner = Evaluation(self)
        try:
            valid = runner.visit(self.entry, instance, '#', 0)
        except Stopped as exc:
            return {**report, 'status': exc.status, 'valid': None, 'errors': [],
                    'reason': {'code': exc.code, 'message': str(exc), 'schema_pointer': exc.path},
                    'evaluation_steps': runner.steps}
        # Evidence of failure belongs only to the overall result. Failed anyOf/not/
        # contains probes must not be exposed as if the instance was rejected.
        return {**report, 'status': 'valid' if valid else 'invalid', 'valid': valid,
                'evaluation_steps': runner.steps, 'errors': [] if valid else [runner.last_failure or {
                    'keyword': 'schema', 'schema_pointer': self.entry, 'instance_pointer': '#'}]}


class Evaluation:
    def __init__(self, schema):
        self.compiled, self.limits = schema, schema.limits
        self.steps = 0
        self.active = set()
        self.last_failure = None

    def tick(self, path):
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise Stopped('limit_exceeded', 'evaluation_capacity', 'Evaluation step capacity exceeded', path)

    def failure(self, path, instance_path, key):
        self.last_failure = {'schema_pointer': path + '/' + escape(key), 'instance_pointer': instance_path, 'keyword': key}
        return False

    def visit(self, path, value, ipath, depth):
        self.tick(path)
        if len(path) > self.limits.max_pointer_characters or len(ipath) > self.limits.max_pointer_characters:
            raise Stopped('limit_exceeded', 'pointer_capacity', 'Evaluation location exceeds capacity')
        if depth > self.limits.max_depth:
            raise Stopped('limit_exceeded', 'evaluation_depth', 'Evaluation recursion exceeds capacity', path)
        pair = (path, ipath)
        if pair in self.active:
            raise Stopped('unsupported', 'non_progressing_reference', 'Recursive reference does not descend through instance data', path)
        self.active.add(pair)
        try:
            return self._visit(path, value, ipath, depth)
        finally:
            self.active.remove(pair)

    def _visit(self, path, value, ipath, depth):
        schema = self.compiled.nodes[path]
        if type(schema) is bool:
            if not schema:
                self.last_failure = {'schema_pointer': path, 'instance_pointer': ipath, 'keyword': 'false'}
            return schema
        def apply(child, item=value, where=ipath):
            return self.visit(child, item, where, depth + 1)
        def fail(key):
            return self.failure(path, ipath, key)
        if '$ref' in schema and not apply(self.compiled.refs[path]):
            return False
        if 'type' in schema:
            kinds = [schema['type']] if type(schema['type']) is str else schema['type']
            matches = {'null': value is None, 'boolean': type(value) is bool, 'integer': integral(value),
                       'number': number(value), 'string': type(value) is str, 'array': type(value) is list, 'object': type(value) is dict}
            if not any(matches[k] for k in kinds):
                return fail('type')
        if 'const' in schema and identity(value) != identity(schema['const']):
            return fail('const')
        if 'enum' in schema:
            found = False
            key = identity(value)
            for item in schema['enum']:
                self.tick(path)
                if key == identity(item):
                    found = True
                    break
            if not found:
                return fail('enum')
        if number(value):
            for key, predicate in [('minimum', lambda a, b: a >= b), ('maximum', lambda a, b: a <= b),
                                   ('exclusiveMinimum', lambda a, b: a > b), ('exclusiveMaximum', lambda a, b: a < b)]:
                if key in schema and not predicate(value, schema[key]):
                    return fail(key)
            if 'multipleOf' in schema and (Fraction(value) / Fraction(schema['multipleOf'])).denominator != 1:
                return fail('multipleOf')
        if type(value) in (str, list, dict):
            names = {str: ('minLength', 'maxLength'), list: ('minItems', 'maxItems'), dict: ('minProperties', 'maxProperties')}[type(value)]
            if names[0] in schema and len(value) < schema[names[0]]:
                return fail(names[0])
            if names[1] in schema and len(value) > schema[names[1]]:
                return fail(names[1])
        if isinstance(value, dict):
            if any(name not in value for name in schema.get('required', [])):
                return fail('required')
            for name, required in schema.get('dependentRequired', {}).items():
                self.tick(path)
                if name in value and any(other not in value for other in required):
                    return fail('dependentRequired')
            for name, item in value.items():
                self.tick(path)
                where = ipath + '/' + escape(name)
                if 'propertyNames' in schema and not apply(path + '/propertyNames', name, where):
                    return False
                if name in schema.get('properties', {}):
                    if not apply(path + '/properties/' + escape(name), item, where):
                        return False
                elif 'additionalProperties' in schema and not apply(path + '/additionalProperties', item, where):
                    return False
            for name in schema.get('dependentSchemas', {}):
                if name in value and not apply(path + '/dependentSchemas/' + escape(name)):
                    return False
        if isinstance(value, list):
            if schema.get('uniqueItems'):
                seen = set()
                for item in value:
                    self.tick(path)
                    key = identity(item)
                    if key in seen:
                        return fail('uniqueItems')
                    seen.add(key)
            prefix = schema.get('prefixItems', [])
            for i, item in enumerate(value):
                self.tick(path)
                target = path + '/prefixItems/' + str(i) if i < len(prefix) else path + '/items' if 'items' in schema else None
                if target and not apply(target, item, ipath + '/' + str(i)):
                    return False
            if 'contains' in schema:
                count = sum(apply(path + '/contains', item, ipath + '/' + str(i)) for i, item in enumerate(value))
                if count < schema.get('minContains', 1) or ('maxContains' in schema and count > schema['maxContains']):
                    return fail('contains')
        for keyword in ('allOf', 'anyOf', 'oneOf'):
            if keyword not in schema:
                continue
            results = [apply(path + '/' + keyword + '/' + str(i)) for i in range(len(schema[keyword]))]
            if (keyword == 'allOf' and not all(results)) or (keyword == 'anyOf' and not any(results)) or (keyword == 'oneOf' and sum(results) != 1):
                return fail(keyword)
        if 'not' in schema and apply(path + '/not'):
            return fail('not')
        if 'if' in schema:
            choice = 'then' if apply(path + '/if') else 'else'
            if choice in schema and not apply(path + '/' + choice):
                return False
        return True


def check(schema_bytes: bytes, instance_bytes: bytes | None = None, *, schema_pointer: str = '#',
          instance_pointer: str = '#', dialect: str | None = None, limits: Limits = DEFAULT_LIMITS) -> dict:
    """Return a non-certifying diagnostic; None is NEVER an invalid-example success."""
    try:
        root = exact_json(schema_bytes, limits)
        compiled = Schema(root, schema_pointer, dialect, limits)
        if instance_bytes is None:
            return {**compiled.report(), 'status': compiled.status, 'valid': None,
                    'evaluation_steps': 0, 'instance_evaluated': False}
        value = exact_json(instance_bytes, limits)
        try:
            _, selected = select(value, instance_pointer)
        except Stopped as exc:
            if exc.status == 'limit_exceeded':
                raise
            raise Stopped('invalid_instance_selection', exc.code, str(exc), exc.path) from exc
        return {**compiled.evaluate(selected), 'instance_evaluated': compiled.status == 'supported'}
    except Stopped as exc:
        return {'engine': ENGINE, 'status': exc.status, 'valid': None,
                'reason': {'code': exc.code, 'message': str(exc), 'pointer': exc.path},
                'complete_standard_conformance': False, 'compatibility_proven': False,
                'semantic_review_required': True, 'limits': asdict(limits), 'instance_evaluated': False}
