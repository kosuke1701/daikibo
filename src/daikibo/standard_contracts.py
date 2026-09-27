"""Lossless, reference-bound OpenAPI / JSON Schema intake, NOT a full validator.

The standard document remains an immutable raw source. Inventory, local JSON
Pointers, effective operation metadata and a conservative change report support
an agent's review. Unknown dialects/references/features are recorded, not erased
or translated into the finite `daikibo.type.v1` language. No network dereference,
implicit adoption, generation of successful contract tests or schema weakening.
"""
from __future__ import annotations

import re
import urllib.parse

from .common import Fault, canonical, digest, need, obj, parse_json, strings, text

FORMAT = 'daikibo.standard-contract.v1'
METHODS = {'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'}
DIALECTS = {'https://json-schema.org/draft/2020-12/schema',
            'http://json-schema.org/draft-07/schema#',
            'https://json-schema.org/draft-07/schema#'}
MAX_NODES = 200_000
MAX_DEPTH = 96
MAX_ENTRIES = 20_000
MAX_MATERIAL = 8_000_000


def escape(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def pointer(root, path):
    """RFC6901 pointer selection only; not JSON Schema base-URI resolution."""
    need(isinstance(path, str) and (path == '#' or path.startswith('#/')),
         'invalid_pointer', 'A local JSON Pointer fragment is required')
    value = root
    if path == '#':
        return value
    try:
        fragment = urllib.parse.unquote(path[1:], errors='strict')
        for part in fragment[1:].split('/'):
            need(not re.search(r'~(?![01])', part), 'invalid_pointer', 'Invalid pointer escape')
            name = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                need(re.fullmatch(r'0|[1-9][0-9]*', name), 'invalid_pointer', 'Invalid array index')
                value = value[int(name)]
            else:
                need(isinstance(value, dict) and name in value, 'unresolved_reference', 'Pointer target is missing', path)
                value = value[name]
        return value
    except (IndexError, ValueError, UnicodeError) as exc:
        raise Fault('unresolved_reference', 'Invalid or missing pointer target', path) from exc


def walk(value, start='#'):
    stack = [(start, value, 0)]
    count = 0
    while stack:
        path, node, depth = stack.pop()
        count += 1
        need(count <= MAX_NODES and depth <= MAX_DEPTH, 'contract_capacity',
             'Contract traversal exceeds explicit node/depth limits; nothing is silently truncated')
        yield path, node
        if isinstance(node, dict):
            stack.extend((path + '/' + escape(k), v, depth + 1) for k, v in reversed(list(node.items())))
        elif isinstance(node, list):
            stack.extend((path + '/' + str(i), v, depth + 1) for i, v in reversed(list(enumerate(node))))


def reference_inventory(root):
    result = []
    # A nested $id can change the resource in which a fragment is interpreted.
    # Do not mislabel a root-pointer lookup as true schema reference resolution.
    nodes = list(walk(root))
    scoped = any(path != '#' and isinstance(node, dict) and '$id' in node for path, node in nodes)
    for path, node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ('$ref', '$dynamicRef', '$recursiveRef', 'operationRef'):
            if key not in node:
                continue
            ref = node[key]
            item = {'pointer': path + '/' + escape(key), 'reference': ref,
                    'status': 'unresolved', 'interpretation': 'syntactic_occurrence_not_semantic_validation'}
            if not isinstance(ref, str):
                item['reason'] = 'reference_not_string'
            elif key in {'$dynamicRef', '$recursiveRef'}:
                item['reason'] = 'dynamic_scope_requires_standard_tool_or_review'
            elif scoped:
                item['reason'] = 'nested_base_uri_requires_standard_resolution'
            elif ref == '#' or ref.startswith('#/'):
                try:
                    item.update(status='local_pointer_found', target_digest=digest(pointer(root, ref)))
                except Fault as exc:
                    item['reason'] = exc.code
            else:
                item['reason'] = 'external_or_anchor_reference_not_resolved'
            result.append(item)
    return result


def inventory(root, standard='auto'):
    need(standard in {'auto', 'openapi', 'jsonschema'}, 'unsupported_standard', 'Use openapi or jsonschema')
    if standard == 'auto':
        need(not (isinstance(root, dict) and 'asyncapi' in root), 'unsupported_standard',
             'AsyncAPI bytes are retained but this inventory does not interpret AsyncAPI')
        standard = 'openapi' if isinstance(root, dict) and 'openapi' in root else 'jsonschema'
    need(isinstance(root, dict) or (standard == 'jsonschema' and type(root) is bool),
         'invalid_contract_document', 'Expected an object or a JSON Schema boolean')
    entries, issues = [], []
    version = root.get('openapi') if standard == 'openapi' else root.get('$schema') if isinstance(root, dict) else None
    refs = reference_inventory(root)
    def add(path, kind, **details):
        need(len(entries) < MAX_ENTRIES, 'contract_capacity', 'Too many contract entries')
        value = pointer(root, path)
        entries.append({'pointer': path, 'kind': kind, 'digest': digest(value),
                        'bytes': len(canonical(value)), **details})
    if standard == 'openapi':
        need(isinstance(version, str) and re.fullmatch(r'3\.[01]\.\d+', version),
             'unsupported_standard_version', 'Operation inventory supports OpenAPI 3.0.x/3.1.x; raw document remains available')
        need(isinstance(root.get('info'), dict), 'invalid_contract_document', 'OpenAPI info must be present')
        for area in ('paths', 'webhooks'):
            paths = root.get(area, {})
            need(isinstance(paths, dict), 'invalid_contract_document', 'Path collection must be an object')
            for name, item in sorted(paths.items()):
                if name.startswith('x-'):
                    continue
                where = '#/' + area + '/' + escape(name)
                need(isinstance(item, dict), 'invalid_contract_document', 'Path Item must be an object', where)
                if '$ref' in item:
                    # Never lose ref-only Path Items from the inventory; resolving
                    # a ref can bring additional operations and conflicting siblings.
                    add(where, 'path_item_reference', route=name)
                    issues.append({'pointer': where, 'reason': 'path_item_reference_requires_resolution'})
                for method, operation in sorted(item.items()):
                    if method not in METHODS:
                        continue
                    need(isinstance(operation, dict), 'invalid_contract_document', 'Operation must be an object')
                    add(where + '/' + method, 'operation' if area == 'paths' else 'webhook',
                        method=method.upper(), route=name, operation_id=operation.get('operationId'))
                    if not isinstance(operation.get('responses'), dict) or not operation['responses']:
                        issues.append({'pointer': where + '/' + method, 'reason': 'responses_missing_or_invalid'})
                    if 'callbacks' in operation:
                        issues.append({'pointer': where + '/' + method + '/callbacks',
                                       'reason': 'callback_operations_preserved_in_entry_not_flattened'})
        components = root.get('components', {})
        need(isinstance(components, dict), 'invalid_contract_document', 'Components must be an object')
        schemas = components.get('schemas', {})
        need(isinstance(schemas, dict), 'invalid_contract_document', 'Schema collection must be an object')
        for name in sorted(schemas):
            add('#/components/schemas/' + escape(name), 'schema')
        ids = [e['operation_id'] for e in entries if e.get('operation_id') is not None]
        need(all(isinstance(x, str) for x in ids), 'invalid_contract_document', 'operationId must be a string')
        if len(ids) != len(set(ids)):
            issues.append({'pointer': '#/paths', 'reason': 'duplicate_operation_id'})
    else:
        add('#', 'schema')
        if version not in DIALECTS:
            issues.append({'pointer': '#/$schema', 'reason': 'unspecified_or_unrecognized_dialect'})
        for group in ('$defs', 'definitions'):
            definitions = root.get(group, {}) if isinstance(root, dict) else {}
            need(isinstance(definitions, dict), 'invalid_contract_document', 'Definitions must be an object')
            for name in sorted(definitions):
                add('#/' + group + '/' + escape(name), 'schema')
    issues.extend({'pointer': r['pointer'], 'reason': r['reason']} for r in refs if r['status'] == 'unresolved')
    return {'standard': standard, 'version': version, 'entries': entries, 'issues': issues, 'references': refs,
            'document_structurally_validated': False, 'semantic_review_required': True,
            'limitation': 'Inventory and literal pointer lookup only; no complete OpenAPI/JSON Schema validation or compatibility proof.'}


def material(root, catalog, selected):
    matches = [e for e in catalog['entries'] if e['pointer'] == selected]
    need(len(matches) == 1, 'unknown_contract_entry', 'Select a pointer from the complete inventory')
    entry = matches[0]
    result = {'format': FORMAT, 'standard': catalog['standard'], 'version': catalog['version'],
              'entry': entry, 'definition': pointer(root, selected), 'context': {},
              'resolved_local_nodes': {}, 'issues': catalog['issues'],
              'semantic_review_required': True, 'document_structurally_validated': False}
    if catalog['standard'] == 'openapi':
        # Keep inherited operation policy and every global extension. components
        # are available below, by reference and also in the preserved document.
        result['context']['global'] = {k: v for k, v in root.items() if k not in {'paths', 'webhooks', 'components'}}
        result['context']['security_schemes'] = root.get('components', {}).get('securitySchemes', {})
        if entry['kind'] in {'operation', 'webhook'}:
            path = pointer(root, selected.rsplit('/', 1)[0])
            operation = result['definition']
            result['context']['path_item'] = {k: v for k, v in path.items() if k not in METHODS}
            # An explicit [] removes global authentication. Absence inherits it.
            result['context']['effective_security'] = operation.get('security', root.get('security', []))
            result['context']['effective_servers'] = operation.get('servers', path.get('servers', root.get('servers', [])))
            result['context']['parameter_sources'] = {'path': path.get('parameters', []), 'operation': operation.get('parameters', [])}
    # Discover transitive local-pointer targets without expansion or infinite recursion.
    # The documented syntactic scope never claims that $id/$dynamicRef were resolved.
    allowed = {r['reference'] for r in catalog['references'] if r['status'] == 'local_pointer_found'}
    pending = [result['definition'], result['context']]
    seen = {selected}
    while pending:
        value = pending.pop()
        for _, node in walk(value):
            if not isinstance(node, dict):
                continue
            ref = node.get('$ref')
            if isinstance(ref, str) and ref in allowed and ref not in seen:
                seen.add(ref)
                target = pointer(root, ref)
                result['resolved_local_nodes'][ref] = target
                pending.append(target)
        need(len(canonical(result)) <= MAX_MATERIAL, 'contract_capacity', 'Entry plus reference context exceeds material limit')
    return result


class StandardContracts:
    def __init__(self, control):
        self.c, self.s = control, control.s

    def _load(self, actor, document, expected_digest, standard):
        row = self.c.documents.get(actor, document)
        raw_digest = row['body']['raw_digest']
        need(expected_digest == raw_digest, 'stale_document', 'Exact original-document digest is required')
        raw = self.s.blob_get(raw_digest)
        need(digest(raw) == raw_digest, 'corrupt_document', 'Original bytes do not match')
        try:
            root = parse_json(raw.decode('utf-8-sig'), limit=MAX_MATERIAL)
            canonical(root)
        except (Fault, ValueError, UnicodeError, RecursionError) as exc:
            raise Fault('unsupported_contract_encoding',
                        'Use UTF-8 JSON serialization for standard inspection. Original bytes are retained; no guessed YAML conversion.') from exc
        return row, root, inventory(root, standard)

    def inspect(self, actor, document, expected_digest, standard='auto', section='entries', offset=0, limit=50):
        row, root, catalog = self._load(actor, document, expected_digest, standard)
        need(section in {'entries', 'issues', 'references'}, 'invalid_section', 'Unknown inventory section')
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             'invalid_range', 'Invalid inventory page')
        values = catalog[section]
        end = min(len(values), offset + limit)
        return {k: v for k, v in catalog.items() if k not in {'entries', 'issues', 'references'}} | {
            'document': document, 'raw_digest': expected_digest, 'section': section,
            'totals': {key: len(catalog[key]) for key in ('entries', 'issues', 'references')},
            'items': values[offset:end], 'next_offset': end if end < len(values) else None,
            'adopted': False}

    def read(self, actor, document, expected_digest, entry, standard='auto', start=0, limit=16000):
        _, root, catalog = self._load(actor, document, expected_digest, standard)
        value = material(root, catalog, entry)
        data = canonical(value).decode()
        need(type(start) is int and start >= 0 and type(limit) is int and 1 <= limit <= 100000,
             'invalid_range', 'Invalid material range')
        end = min(len(data), start + limit)
        return {'document': document, 'raw_digest': expected_digest, 'entry': entry, 'digest': digest(value),
                'content': data[start:end], 'total_characters': len(data), 'start': start, 'end': end,
                'next_start': end if end < len(data) else None, 'semantic_review_required': True}

    def propose(self, actor, document, expected_digest, entry, semantics, standard='auto', artifact_id=None):
        row, root, catalog = self._load(actor, document, expected_digest, standard)
        actor.require('owner', 'agent', project=row['project'])
        obj(semantics, required=('title', 'statement', 'idempotency', 'compatibility', 'consumers', 'verification'))
        for name in ('title', 'statement', 'idempotency', 'compatibility'):
            text(semantics[name], name, 400 if name == 'title' else 200000)
        strings(semantics['consumers'], 'consumers')
        strings(semantics['verification'], 'verification', nonempty=True)
        value = material(root, catalog, entry)
        binding = {'format': FORMAT, 'document': document, 'raw_digest': expected_digest,
                   'entry': entry, 'material_digest': digest(value), 'standard': catalog['standard'],
                   'version': catalog['version'], 'issue_count': len(catalog['issues']),
                   'complete_standard_validation': False}
        # No lossy projection to a permissive synthetic input/output schema.
        body = {**semantics, 'input': {**binding, 'part': 'request_or_schema'},
                'output': {**binding, 'part': 'all_responses_or_schema'},
                'authentication': {**binding, 'part': 'effective_security_and_schemes'},
                'errors': {**binding, 'part': 'responses_and_unresolved_references'},
                'standard_contract': binding, 'standard_contract_material': value, 'source_refs': [row['body']['text_source']['id']] if row['body'].get('text_source') else []}
        with self.s.transaction():
            draft = self.c.k.propose(actor, row['project'], 'interface', body, artifact_id=artifact_id)
            self.c.sec.event(row['project'], 'standard_contract_draft_imported', actor.id,
                             {'artifact': draft['id'], **binding})
        return draft

    def compare(self, actor, before, before_digest, after, after_digest, standard='auto', offset=0, limit=100):
        a, root_a, cat_a = self._load(actor, before, before_digest, standard)
        b, root_b, cat_b = self._load(actor, after, after_digest, standard)
        need(a['project'] == b['project'], 'cross_project', 'Compare contracts within one project')
        need(cat_a['standard'] == cat_b['standard'], 'incompatible_standard', 'Document types differ')
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
             'invalid_range', 'Invalid change page')
        paths_a = {e['pointer']: e for e in cat_a['entries']}
        paths_b = {e['pointer']: e for e in cat_b['entries']}
        changes = []
        for path in sorted(paths_a.keys() | paths_b.keys()):
            kind = 'added' if path not in paths_a else 'removed' if path not in paths_b else None
            if kind is None and digest(material(root_a, cat_a, path)) != digest(material(root_b, cat_b, path)):
                kind = 'changed'
            if kind:
                changes.append({'entry': path, 'change': kind})
        end = min(len(changes), offset + limit)
        return {'before': before, 'after': after, 'before_digest': before_digest, 'after_digest': after_digest,
                'byte_identical': before_digest == after_digest, 'total_changes': len(changes),
                'changes': changes[offset:end], 'next_offset': end if end < len(changes) else None,
                'type_compatible_proven': False, 'semantic_review_required': True,
                'before_issue_count': len(cat_a['issues']), 'after_issue_count': len(cat_b['issues']),
                'unindexed_document_changes_possible': digest(root_a) != digest(root_b)}


def verify_projection(store, project, body):
    """Check generated interface material against retained immutable original bytes.

    Called at proposal/revision and normal artifact reads (and thus Gate reads).
    This is provenance consistency, never standard validity or semantic approval.
    """
    if 'standard_contract' not in body:
        return
    binding = body['standard_contract']
    need(isinstance(binding, dict) and binding.get('format') == FORMAT,
         'invalid_contract_projection', 'Unknown standard-contract binding')
    obj(binding, required=('format', 'document', 'raw_digest', 'entry', 'material_digest', 'standard',
                           'version', 'issue_count', 'complete_standard_validation'))
    need(type(binding['issue_count']) is int and binding['issue_count'] >= 0,
         'invalid_contract_projection', 'Invalid issue count')
    need(binding['complete_standard_validation'] is False, 'invalid_contract_projection',
         'Import cannot claim a complete standards validation')
    row = store.one('SELECT project,body FROM documents WHERE id=?', (binding['document'],), True)
    doc = parse_json(row['body'])
    need(row['project'] == project and doc['raw_digest'] == binding['raw_digest'],
         'invalid_contract_projection', 'Document project or byte identity differs')
    raw = store.blob_get(binding['raw_digest'])
    root = parse_json(raw.decode('utf-8-sig'), limit=MAX_MATERIAL)
    cat = inventory(root, binding['standard'])
    computed = material(root, cat, binding['entry'])
    need(binding['version'] == cat['version'] and binding['issue_count'] == len(cat['issues'])
         and binding['material_digest'] == digest(computed)
         and digest(body.get('standard_contract_material')) == digest(computed),
         'invalid_contract_projection', 'Derived contract was edited or no longer matches its source')
    for key, part in {'input': 'request_or_schema', 'output': 'all_responses_or_schema',
                      'authentication': 'effective_security_and_schemes',
                      'errors': 'responses_and_unresolved_references'}.items():
        need(body.get(key) == {**binding, 'part': part}, 'invalid_contract_projection',
             'Do not replace imported protocol details with an unchecked synthetic contract')
    expected_sources = [doc['text_source']['id']] if doc.get('text_source') else []
    need(body.get('source_refs') == expected_sources, 'invalid_contract_projection', 'Source lineage was removed')
