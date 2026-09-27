"""Local operational journal and log redaction. No access-control boundary."""
from __future__ import annotations
import hashlib
import hmac
import os
import re
import secrets
from .common import Actor, Fault, atomic_write, canonical, digest, need, parse_json, timestamp, uid

ROLES = {"owner", "agent", "worker", "reviewer", "observer"}
REDACTIONS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{15,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
]

def redact(value: str, secrets_to_hide=()) -> str:
    for secret in secrets_to_hide:
        if secret and len(secret) >= 4:
            value = value.replace(secret, "[REDACTED]")
    for pattern in REDACTIONS:
        value = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", value)
    return value

class Security:
    """Compatibility name for the local journal, NOT a security boundary.

    New records use unkeyed hashes for corruption/staleness checks. Legacy HMAC
    records can still be read so an upgrade never silently discards old evidence.
    """
    def __init__(self, store, clock=timestamp):
        self.s, self.clock = store, clock
        self.keyfile = store.home / 'keys.json'
        self.keys = parse_json(self.keyfile.read_bytes()) if self.keyfile.exists() else None

    def mac(self, body, keyid=None):
        # Column names are retained for database compatibility.
        if keyid and keyid != 'sha256-unkeyed-v1':
            need(self.keys and keyid in self.keys['keys'], 'missing_key', 'Legacy evidence key is unavailable')
            return keyid, hmac.new(bytes.fromhex(self.keys['keys'][keyid]), canonical(body), hashlib.sha256).hexdigest()
        return 'sha256-unkeyed-v1', digest(body)

    def verify(self, body, keyid, mac):
        _, expected = self.mac(body, keyid)
        need(isinstance(mac, str) and hmac.compare_digest(expected, mac), 'invalid_evidence', 'Stored observation checksum differs')

    def bootstrap(self):
        # A non-secret migration marker. No owner token or signing key is created.
        path = self.s.home / 'local.identity'
        if not path.exists():
            atomic_write(path, b'local\n')
            self.event(None, 'cooperative_control_initialized', 'local-user',
                       {'execution_model':'cooperative-single-user','authentication':False})
        return str(path)

    def authenticate(self, token=None):
        # Old RPC clients may still send token; it has no authority in this mode.
        return Actor('local-user', 'owner')

    def event(self, project, kind, actor, body):
        with self.s.transaction():
            prev = self.s.one('SELECT mac FROM events ORDER BY seq DESC LIMIT 1')
            record = {'id':uid('EVT'), 'project':project, 'kind':kind, 'actor':actor,
                      'body':body, 'created':self.clock(), 'previous':prev['mac'] if prev else '0'*64}
            keyid, checksum = self.mac(record)
            self.s.execute('INSERT INTO events(id,project,kind,actor,body,created,previous,key_id,mac) VALUES(?,?,?,?,?,?,?,?,?)',
                           (record['id'], project, kind, actor, canonical(body).decode(), record['created'], record['previous'], keyid, checksum))
            return record['id']

    def audit(self):
        previous, count = '0'*64, 0
        for row in self.s.all('SELECT * FROM events ORDER BY seq'):
            need(row['previous']==previous, 'audit_tampering', 'Journal checksum chain is inconsistent')
            body = {k:row[k] for k in ('id','project','kind','actor','created','previous')}
            body['body']=parse_json(row['body'])
            self.verify(body,row['key_id'],row['mac']); previous=row['mac']; count+=1
        return {'events':count,'head':previous,'verified':True,'tamper_resistant':False,
                'limitation':'Checks detect missing or altered records during normal operation; a same-user process can rewrite the database and checksums.'}
