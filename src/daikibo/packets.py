"""Lossless bounded source packets: segmentation is not semantic decomposition."""
from __future__ import annotations
from .common import canonical, digest, need, parse_json


def slices(content, byte_budget):
    """Yield exact character spans, preserving Unicode and every byte of the input."""
    need(type(byte_budget) is int and byte_budget >= 256, 'invalid_budget', 'At least 256 UTF-8 bytes are required')
    start = 0
    while start < len(content):
        part = content[start:start + byte_budget].encode()[:byte_budget].decode('utf-8', errors='ignore')
        need(part, 'invalid_budget', 'Budget cannot hold a character')
        # Prefer a nearby complete line, without dropping its newline.
        cut = part.rfind('\n') + 1
        if cut >= len(part)//2 and cut > 0: part = part[:cut]
        end = start + len(part)
        yield start, end, part
        start = end


class SourcePackets:
    def __init__(self, c): self.c, self.s = c, c.s

    def partition(self, actor, source, byte_budget=20000, offset=0, limit=100):
        src = self.s.one('SELECT * FROM sources WHERE id=?', (source,), True)
        self.c.k.project(actor, src['project'])
        need(type(byte_budget) is int and 256 <= byte_budget <= 200000, 'invalid_budget', 'Budget must be 256..200000 UTF-8 bytes')
        need(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200, 'invalid_range', 'Invalid page')
        key = 'source-packets:' + source + ':' + str(byte_budget)
        saved = self.s.one('SELECT value FROM meta WHERE key=?', (key,))
        if saved:
            manifest = parse_json(saved['value'])
        else:
            content = self.s.blob_get(src['blob']).decode()
            packets = []
            for start, end, part in slices(content, byte_budget):
                packets.append({'source':source, 'source_digest':src['blob'], 'start':start, 'end':end,
                                'digest':digest(part.encode()), 'bytes':len(part.encode())})
            manifest = {'source':source,'source_digest':src['blob'],'characters':len(content),
                        'byte_budget':byte_budget,'packets':packets,'text_coverage_complete':True,
                        'semantic_requirements_extracted':False}
            with self.s.transaction():
                self.s.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,canonical(manifest).decode()))
                self.c.sec.event(src['project'],'source_partitioned',actor.id,
                                 {'source':source,'packets':len(packets),'original_preserved':True,'semantic_completion':False})
        parts = manifest['packets']
        return {k:v for k,v in manifest.items() if k!='packets'} | {
            'packets':parts[offset:offset+limit], 'total':len(parts),
            'next_offset':offset+limit if offset+limit < len(parts) else None}

    def packet(self, actor, source, start, end, expected_digest):
        src = self.s.one('SELECT * FROM sources WHERE id=?', (source,), True)
        self.c.k.project(actor, src['project'])
        need(type(start) is int and type(end) is int and 0 <= start < end <= src['characters'], 'invalid_range', 'Invalid packet range')
        value = self.s.blob_get(src['blob']).decode()[start:end]
        need(len(value.encode()) <= 200000, 'context_insufficient', 'Request a bounded source packet')
        need(digest(value.encode()) == expected_digest, 'stale_packet', 'Packet digest differs')
        dispositions = self.s.all('SELECT start,end,category,refs,reason FROM dispositions WHERE source=? AND start<? AND end>? ORDER BY start', (source,end,start))
        return {'source':source,'start':start,'end':end,'digest':expected_digest,'content':value,'dispositions':dispositions}

    def status(self, actor, source, byte_budget=20000):
        src = self.s.one('SELECT * FROM sources WHERE id=?', (source,), True)
        self.c.k.project(actor, src['project'])
        content = self.s.blob_get(src['blob']).decode()
        spans = self.s.all('SELECT start,end FROM dispositions WHERE source=? ORDER BY start', (source,))
        gaps, cursor = [], 0
        for span in spans:
            if span['start'] > cursor and content[cursor:span['start']].strip(): gaps.append([cursor,span['start']])
            cursor = max(cursor, span['end'])
        if cursor < len(content) and content[cursor:].strip(): gaps.append([cursor,len(content)])
        return {'source':source,'unclassified_ranges':gaps,'classified_complete':not gaps,
                'meaning_review_still_required':True,'next':'source.read or source.partition, then classify each original span'}
