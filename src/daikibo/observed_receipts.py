"""Durable ordering for immutable Runtime observations.

Receipt ``created`` values are wall-clock telemetry and may move backwards.
Runtime commits a ``run_observed`` journal event in the same transaction as
each receipt, so the event sequence is the durable observation order for the
finite read consumers that select the latest review or formal check.
"""
from __future__ import annotations

from typing import Any, Iterable
import weakref

from .common import Fault, digest, parse_json


_EVENT_CHAIN_CACHE: weakref.WeakKeyDictionary[Any, tuple[Any, list[tuple[dict[str, Any], dict[str, Any]]]]] = weakref.WeakKeyDictionary()


def _invalid(message: str, details: Any = None) -> None:
    raise Fault("observed_order_invalid", message, details)


def _validated_events(control: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Validate the durable event journal once per unchanged Control state."""
    rows = control.s.all("SELECT * FROM events ORDER BY seq")
    fingerprint_parts = []
    for row in rows:
        try:
            body_digest = digest(parse_json(row["body"]))
        except Fault as exc:
            raise Fault("observed_order_invalid", "Observed event body is malformed", row.get("id")) from exc
        fingerprint_parts.append(
            (row.get("seq"), row.get("id"), row.get("project"), row.get("kind"),
             row.get("actor"), row.get("created"), row.get("previous"),
             row.get("key_id"), row.get("mac"), body_digest)
        )
    fingerprint = tuple(fingerprint_parts)
    cached = _EVENT_CHAIN_CACHE.get(control)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    previous = "0" * 64
    expected_seq = 1
    validated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if row.get("seq") != expected_seq:
            _invalid("Observed event sequence has a gap", {"expected": expected_seq, "actual": row.get("seq")})
        try:
            body = parse_json(row["body"])
        except Fault as exc:
            raise Fault("observed_order_invalid", "Observed event body is malformed", row.get("id")) from exc
        if not isinstance(body, dict):
            _invalid("Observed event body is not an object", row.get("id"))
        if row.get("previous") != previous:
            _invalid("Observed event MAC chain predecessor differs", row.get("id"))
        record = {key: row[key] for key in ("id", "project", "kind", "actor", "created", "previous")}
        record["body"] = body
        try:
            control.sec.verify(record, row["key_id"], row["mac"])
        except Fault as exc:
            raise Fault("observed_order_invalid", "Observed event MAC differs", row.get("id")) from exc
        validated.append((row, body))
        previous = row["mac"]
        expected_seq += 1
    _EVENT_CHAIN_CACHE[control] = (fingerprint, validated)
    return validated


def _candidate_rows(control: Any, *, project: str, subject: str, role: str,
                    binding: str | None, receipt_ids: Iterable[str] | None) -> list[dict[str, Any]]:
    if receipt_ids is None:
        if binding is None:
            return control.s.all(
                "SELECT * FROM receipts WHERE project=? AND subject=? AND role=? ORDER BY id",
                (project, subject, role),
            )
        return control.s.all(
            "SELECT * FROM receipts WHERE project=? AND subject=? AND role=? AND binding=? ORDER BY id",
            (project, subject, role, binding),
        )
    identifiers = list(receipt_ids)
    if any(type(ident) is not str or not ident for ident in identifiers):
        _invalid("Observed receipt candidate identifier is invalid")
    if len(identifiers) != len(set(identifiers)):
        _invalid("Observed receipt candidate list contains duplicates")
    rows = []
    for ident in identifiers:
        row = control.s.one("SELECT * FROM receipts WHERE id=?", (ident,))
        if row is None:
            _invalid("Observed receipt candidate is missing", ident)
        rows.append(row)
    return rows


def ordered_observed_receipts(control: Any, *, project: str, subject: str, role: str,
                              binding: str | None = None,
                              receipt_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Return candidate receipts in durable ``events.seq`` order.

    Every candidate is checked against its receipt row, finished run, observed
    body, MAC, and exactly one same-transaction ``run_observed`` event. Missing
    or duplicate event links are errors; callers therefore keep their existing
    fail-closed status handling instead of falling back to wall-clock order.
    """
    rows = _candidate_rows(control, project=project, subject=subject, role=role,
                           binding=binding, receipt_ids=receipt_ids)
    if not rows:
        return []
    events = _validated_events(control)
    event_rows = [(row, body) for row, body in events if row.get("kind") == "run_observed"
                  and row.get("project") == project]
    ordered: list[dict[str, Any]] = []
    for row in rows:
        if (row.get("project") != project or row.get("subject") != subject or
                row.get("role") != role or (binding is not None and row.get("binding") != binding)):
            _invalid("Observed receipt row identity differs", row.get("id"))
        try:
            body = parse_json(row["body"])
        except Fault as exc:
            raise Fault("observed_order_invalid", "Observed receipt body is malformed", row.get("id")) from exc
        if (not isinstance(body, dict) or body.get("id") != row.get("id") or
                body.get("run") != row.get("run") or body.get("project") != project or
                body.get("subject") != subject or body.get("role") != role or
                body.get("binding") != row.get("binding")):
            _invalid("Observed receipt row/body identity differs", row.get("id"))
        try:
            control.sec.verify(body, row["key_id"], row["mac"])
        except Fault as exc:
            raise Fault("observed_order_invalid", "Observed receipt MAC differs", row.get("id")) from exc
        run = control.s.one("SELECT * FROM runs WHERE id=?", (row.get("run"),))
        if (run is None or run.get("project") != project or run.get("subject") != subject or
                run.get("role") != role or run.get("binding") != row.get("binding") or
                run.get("status") != "finished"):
            _invalid("Observed receipt run identity or completion differs", row.get("id"))
        matches = []
        for event_row, event_body in event_rows:
            if event_body.get("receipt") == row.get("id") or event_body.get("run") == row.get("run"):
                matches.append((event_row, event_body))
        if len(matches) != 1:
            _invalid("Observed receipt does not have exactly one durable run event", row.get("id"))
        event_row, event_body = matches[0]
        if event_row.get("actor") != "collector":
            _invalid("Observed run event actor differs", row.get("id"))
        if (event_body.get("receipt") != row.get("id") or event_body.get("run") != row.get("run") or
                event_body.get("exit") != body.get("exit_code") or
                event_body.get("cancelled") != bool(body.get("cancelled")) or
                event_body.get("timed_out") != bool(body.get("timed_out"))):
            _invalid("Observed run event does not match its receipt", row.get("id"))
        ordered.append({"row": row, "body": body, "event": event_row,
                        "event_body": event_body, "event_seq": event_row["seq"]})
    ordered.sort(key=lambda item: item["event_seq"])
    if len({item["event_seq"] for item in ordered}) != len(ordered):
        _invalid("Observed receipts share a durable event sequence")
    return ordered


__all__ = ["ordered_observed_receipts"]
