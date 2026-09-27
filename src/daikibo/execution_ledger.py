"""Atomic local invocation budgets and explicitly reported (not billed) usage."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .common import canonical, need, number, obj, parse_json, text, timestamp


def usage_values(kind: str, metadata: dict) -> tuple[int | None, int | None]:
    usage = metadata.get('usage')
    total = None
    if isinstance(usage, dict):
        keys = ['input_tokens', 'output_tokens']
        # Claude's cache reads/creation are separate from ordinary input tokens.
        # Codex cached_input_tokens is a subset; do not count that subset twice.
        if kind == 'claude':
            keys += [key for key in ('cache_read_input_tokens', 'cache_creation_input_tokens') if key in usage]
        values = [usage.get(key) for key in keys]
        if all(type(v) is int and 0 <= v <= 10**15 for v in values):
            total = sum(values)
    cost = None
    value = metadata.get('total_cost_usd')
    if type(value) in (int, float, str):
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and 0 <= amount <= 10**9:
                # Conservative rounding avoids turning a reported fractional unit into zero.
                cost = int((amount * 1_000_000).to_integral_value(rounding='ROUND_CEILING'))
        except (InvalidOperation, ValueError):
            pass
    return total, cost


class ExecutionLedger:
    def __init__(self, store, security, knowledge):
        self.s, self.sec, self.k = store, security, knowledge
        self._backfill_observed_runs()

    def _backfill_observed_runs(self):
        """Upgrade old observations without inventing any missing usage."""
        while True:
            rows=self.s.all("""SELECT r.id,r.project,r.adapter,r.status,r.start,
                ev.body AS observation,a.body AS adapter_body
                FROM runs r LEFT JOIN receipts ev ON ev.run=r.id
                LEFT JOIN adapters a ON a.name=r.adapter
                LEFT JOIN execution_usage u ON u.run=r.id
                WHERE u.run IS NULL AND r.adapter!='command' LIMIT 500""")
            if not rows:break
            with self.s.transaction():
                for row in rows:
                    observation=parse_json(row['observation']) if row['observation'] else {}
                    ad=parse_json(row['adapter_body']) if row['adapter_body'] else {}
                    metadata=observation.get('adapter_metadata',{})
                    tokens,cost=usage_values(ad.get('kind','unknown'),metadata)
                    status='observed' if observation else 'unknown'
                    if observation.get('process_started') is False:status='not_started'
                    self.s.execute("INSERT OR IGNORE INTO execution_usage VALUES(?,?,?,?,?,?,?,?,?)",
                        (row['id'],row['project'],row['adapter'],status,tokens,cost,
                         canonical({'migrated_from_existing_observation':bool(observation),'metadata':metadata,
                                    'billed_amount_verified':False}).decode(),row['start'],timestamp()))

    def configure(self, actor, project, limits, reason):
        actor.require('owner', project=project); self.k.project(actor, project)
        text(reason, 'limit change reason', 4000)
        obj(limits, optional=('max_invocations', 'max_reported_tokens', 'max_estimated_cost_usd'))
        cleaned = {}
        for key in ('max_invocations', 'max_reported_tokens'):
            value = limits.get(key)
            if value is not None:
                number(value, key, 1, 10**15, integer=True)
            cleaned[key] = value
        value = limits.get('max_estimated_cost_usd')
        if value is not None:
            number(value, 'max_estimated_cost_usd', 0.000001, 10**9)
        cleaned['max_estimated_cost_usd'] = value
        with self.s.transaction():
            old = self.s.one('SELECT revision FROM execution_limits WHERE project=?', (project,))
            revision = old['revision'] + 1 if old else 1
            self.s.execute('INSERT INTO execution_limits VALUES(?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET revision=excluded.revision,body=excluded.body,reason=excluded.reason,updated=excluded.updated',
                           (project, revision, canonical(cleaned).decode(), reason, timestamp()))
            self.sec.event(project, 'execution_limits_changed', actor.id, {'limits': cleaned, 'revision': revision, 'reason': reason, 'usage_counters_reset': False})
        return self.summary(actor, project)

    def _totals(self, project):
        row = self.s.one('''SELECT count(*) AS invocations,
            coalesce(sum(status='reserved'),0) AS in_flight,
            coalesce(sum(status IN ('observed','unknown') AND tokens IS NULL),0) AS unknown_tokens,
            coalesce(sum(status IN ('observed','unknown') AND cost_microusd IS NULL),0) AS unknown_cost,
            coalesce(sum(tokens),0) AS reported_tokens,
            coalesce(sum(cost_microusd),0) AS reported_cost_microusd,
            coalesce(sum(status='unknown'),0) AS interrupted_invocations
            FROM execution_usage WHERE project=? AND status!='not_started' ''', (project,))
        return row

    def summary(self, actor, project):
        self.k.project(actor, project)
        with self.s.lock:
            configured = self.s.one('SELECT * FROM execution_limits WHERE project=?', (project,))
            totals = self._totals(project)
            rows = self.s.all('''SELECT adapter,count(*) AS invocations FROM execution_usage
                WHERE project=? AND status!='not_started' GROUP BY adapter ORDER BY adapter''', (project,))
        return {'project': project, 'limits': parse_json(configured['body']) if configured else {},
                'revision': configured['revision'] if configured else 0, **totals,
                'reported_estimated_cost_usd': totals['reported_cost_microusd'] / 1_000_000,
                'by_adapter': rows, 'actual_bill_verified': False,
                'semantics': 'Counts managed CLI invocations, not API requests. Token/cost values are CLI-reported. Missing values are unknown, not zero. Caps stop future starts, not an already-running CLI or the provider bill.'}

    def reserve(self, run, project, adapter):
        """Serializes admission with other invocations before Popen, including no-limit runs."""
        with self.s.transaction():
            configured = self.s.one('SELECT body FROM execution_limits WHERE project=?', (project,))
            limits = parse_json(configured['body']) if configured else {}
            totals = self._totals(project)
            reasons = []
            n = limits.get('max_invocations')
            if n is not None and totals['invocations'] >= n:
                reasons.append('invocation_limit')
            n = limits.get('max_reported_tokens')
            if n is not None:
                if totals['reported_tokens'] >= n: reasons.append('reported_token_limit')
                if totals['unknown_tokens']: reasons.append('unknown_token_usage')
            n = limits.get('max_estimated_cost_usd')
            if n is not None:
                if Decimal(totals['reported_cost_microusd']) >= Decimal(str(n)) * 1_000_000:
                    reasons.append('reported_cost_limit')
                if totals['unknown_cost']: reasons.append('unknown_cost_usage')
            if (limits.get('max_reported_tokens') is not None or n is not None) and totals['in_flight']:
                reasons.append('unobserved_in_flight_usage')
            need(not reasons, 'execution_budget', 'Starting another agent invocation requires budget/usage reassessment',
                 {'reasons': reasons, 'technical_impossibility': False})
            self.s.execute("INSERT INTO execution_usage(run,project,adapter,status,body,created) VALUES(?,?,?,'reserved','{}',?)", (run, project, adapter, timestamp()))

    def finish(self, run, kind, metadata, process_started):
        total, cost = usage_values(kind, metadata)
        with self.s.transaction():
            self.s.execute('''UPDATE execution_usage SET status=?,tokens=?,cost_microusd=?,body=?,updated=?
                WHERE run=? AND status='reserved' ''',
                ('observed' if process_started else 'not_started', total, cost,
                 canonical({'cli_kind': kind, 'metadata': metadata, 'process_started': process_started, 'billed_amount_verified': False}).decode(), timestamp(), run))

    def interrupted(self):
        with self.s.transaction():
            return self.s.execute("UPDATE execution_usage SET status='unknown',updated=? WHERE status='reserved'", (timestamp(),)).rowcount
