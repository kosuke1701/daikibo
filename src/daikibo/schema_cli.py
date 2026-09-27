"""Local contract data check for Runtime's existing command/JUnit test plans.

python -m daikibo.schema_cli --schema contract.json --instance response.json \
  --dialect https://json-schema.org/draft/2020-12/schema --report contract.xml

An unsupported schema is an ERROR, including when invalid data was expected.
This CLI doesn't issue trusted receipts or perform semantic/coverage reviews.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import atomic_write, canonical
from .schema_evaluator import DEFAULT_LIMITS, ENGINE, Stopped, check


def read_bounded(path: Path) -> bytes:
    with path.open('rb') as stream:
        value = stream.read(DEFAULT_LIMITS.max_bytes + 1)
    if len(value) > DEFAULT_LIMITS.max_bytes:
        raise Stopped('limit_exceeded', 'byte_capacity', 'Input exceeds byte limit; no prefix digest is accepted')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', required=True, type=Path)
    parser.add_argument('--instance', required=True, type=Path)
    parser.add_argument('--schema-pointer', default='#')
    parser.add_argument('--instance-pointer', default='#')
    parser.add_argument('--dialect')
    parser.add_argument('--expect', choices=('valid', 'invalid'), default='valid')
    parser.add_argument('--name', default='contract-instance')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    # Do not overwrite test inputs with the report, even through a symlink.
    if args.report.resolve() in {args.schema.resolve(), args.instance.resolve()}:
        parser.error('Report path must differ from schema and instance paths')
    binding = {}
    try:
        raw, data = read_bounded(args.schema), read_bounded(args.instance)
        binding = {'schema_sha256': hashlib.sha256(raw).hexdigest(), 'instance_sha256': hashlib.sha256(data).hexdigest(),
                   'schema_pointer': args.schema_pointer, 'instance_pointer': args.instance_pointer}
        result = check(raw, data, schema_pointer=args.schema_pointer, instance_pointer=args.instance_pointer, dialect=args.dialect)
    except Stopped as exc:
        result = {'engine': ENGINE, 'status': exc.status, 'valid': None, 'message': str(exc)}
    except OSError as exc:
        result = {'engine': ENGINE, 'status': 'input_error', 'valid': None, 'message': str(exc)}
    conclusive = type(result.get('valid')) is bool and result['status'] in {'valid', 'invalid'}
    matched = conclusive and result['valid'] is (args.expect == 'valid')
    outcome = {'format': 'daikibo.schema-test.v1', 'binding': binding, 'expected': args.expect,
               'matched': matched, 'conclusive': conclusive, 'result': result,
               'deploy_ready': False, 'semantic_review_required': True}
    root = ET.Element('testsuite', name='daikibo.contract', tests='1',
                      failures=str(int(conclusive and not matched)), errors=str(int(not conclusive)), skipped='0')
    properties = ET.SubElement(root, 'properties')
    for name, value in {'engine': ENGINE, **binding, 'expected': args.expect}.items():
        ET.SubElement(properties, 'property', name=name, value=value)
    case = ET.SubElement(root, 'testcase', name=args.name, classname='daikibo.contract')
    if not matched:
        entry = ET.SubElement(case, 'failure' if conclusive else 'error', message=result['status'])
        entry.text = json.dumps(result, ensure_ascii=True, sort_keys=True)
    ET.SubElement(case, 'system-out').text = json.dumps(outcome, ensure_ascii=True, sort_keys=True)
    atomic_write(args.report, ET.tostring(root, encoding='utf-8', xml_declaration=True))
    print(canonical(outcome).decode())
    return 0 if matched else 1 if conclusive else 2


if __name__ == '__main__':
    sys.exit(main())
