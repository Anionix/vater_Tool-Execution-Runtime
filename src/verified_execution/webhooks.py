"""Inbound event notifications (webhooks): task.optional_more_protocols.009 / 010 / 011.

009  A delivery is accepted only with a valid signature: HMAC-SHA256 of the raw body under
     the shared secret, sent as "sha256=<64 hex digits>" and compared in constant time.
     A missing, malformed or wrong signature is refused before anything is parsed or stored.
010  Each delivery id is recorded once. The same id with the same body is a duplicate and
     is never applied again; the same id with a different body is a conflict and refused.
     Per resource, an event whose sequence is not newer than the last applied one is
     recorded as stale and not applied, so the resulting state is the same in whatever
     order deliveries arrive.
011  A delivery is recorded as received before it is applied, and applying it and marking
     it processed happen in one transaction. A redelivery of a processed id is acknowledged
     without effect (a resend); a redelivery of an id whose processing was interrupted is
     applied now (its result had been lost). reconcile() compares the sender's delivery
     list with the log: deliveries that never arrived are named for redelivery, never
     assumed not to have happened.

Standard library only; the log is a SQLite file of its own, apart from the operation ledger.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-delivery-id"
EVENT_HEADER = "x-event"
_SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    resource TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('received', 'processed', 'stale')),
    received_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    resource TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
"""


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """True only for "sha256=<hex>" equal to HMAC-SHA256(secret, body), compared in constant time."""
    if not secret or not isinstance(header, str):
        return False
    match = _SIGNATURE.fullmatch(header.strip())
    if match is None:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


@dataclass(frozen=True)
class Outcome:
    status: int   # the HTTP status a receiver endpoint answers with
    kind: str     # rejected_signature | malformed | conflict | duplicate | stale | applied | failed


@dataclass(frozen=True)
class _Delivery:
    delivery_id: str
    event: str
    resource: str
    sequence: int
    data: Any
    body_sha256: str


def _parse(headers: Mapping[str, str], body: bytes) -> _Delivery | None:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    delivery_id, event = lowered.get(DELIVERY_HEADER), lowered.get(EVENT_HEADER)
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if (not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery_id)
            or not isinstance(event, str) or not event or not isinstance(payload, dict)
            or not isinstance(payload.get("resource"), str) or not payload["resource"]
            or type(payload.get("sequence")) is not int or payload["sequence"] < 0):
        return None
    return _Delivery(delivery_id, event, payload["resource"], payload["sequence"], payload.get("data"),
                     hashlib.sha256(body).hexdigest())


class WebhookReceiver:
    """Applies each authentic delivery's effect to the local state at most once."""

    def __init__(self, path: str | Path, secret: bytes, apply: Callable[[Any, Any], Any]) -> None:
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("a shared secret of at least 16 bytes is required")
        self.path, self._secret, self._apply = str(path), secret, apply
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0, isolation_level=None)

    def receive(self, headers: Mapping[str, str], body: bytes) -> Outcome:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        if not verify_signature(self._secret, body, lowered.get(SIGNATURE_HEADER)):
            return Outcome(401, "rejected_signature")  # nothing parsed, nothing stored
        delivery = _parse(headers, body)
        if delivery is None:
            return Outcome(400, "malformed")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                outcome = self._record_and_apply(connection, delivery)
            except Exception:
                connection.execute("ROLLBACK")
                self._record_received(delivery)  # the delivery stays received, unapplied
                return Outcome(500, "failed")    # the sender redelivers; it is applied then
            connection.execute("COMMIT")
            return outcome

    def _record_received(self, delivery: _Delivery) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,'received',?)",
                (delivery.delivery_id, delivery.event, delivery.resource, delivery.sequence,
                 delivery.body_sha256, int(time.time() * 1000)))

    def _record_and_apply(self, connection: sqlite3.Connection, delivery: _Delivery) -> Outcome:
        row = connection.execute("SELECT body_sha256, state FROM deliveries WHERE delivery_id=?",
                                 (delivery.delivery_id,)).fetchone()
        if row is not None and row[0] != delivery.body_sha256:
            return Outcome(409, "conflict")
        if row is not None and row[1] in ("processed", "stale"):
            return Outcome(200, "duplicate")  # a resend of a delivery already handled
        if row is None:
            connection.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,'received',?)",
                (delivery.delivery_id, delivery.event, delivery.resource, delivery.sequence,
                 delivery.body_sha256, int(time.time() * 1000)))
        current = connection.execute("SELECT sequence, state_json FROM resources WHERE resource=?",
                                     (delivery.resource,)).fetchone()
        if current is not None and delivery.sequence <= current[0]:
            connection.execute("UPDATE deliveries SET state='stale' WHERE delivery_id=?", (delivery.delivery_id,))
            return Outcome(200, "stale")
        new_state = self._apply(json.loads(current[1]) if current else None, delivery.data)
        connection.execute(
            "INSERT INTO resources VALUES (?,?,?) ON CONFLICT(resource) DO UPDATE SET sequence=excluded.sequence, "
            "state_json=excluded.state_json", (delivery.resource, delivery.sequence, json.dumps(new_state)))
        connection.execute("UPDATE deliveries SET state='processed' WHERE delivery_id=?", (delivery.delivery_id,))
        return Outcome(200, "applied")

    def state(self, resource: str) -> Any:
        """The applied state of a resource, or None if no delivery for it was applied."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT state_json FROM resources WHERE resource=?", (resource,)).fetchone()
        return json.loads(row[0]) if row else None

    def reconcile(self, sender_deliveries: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
        """Compare the sender's delivery list ({"id", "delivered"}) with the log."""
        with closing(self._connect()) as connection:
            logged = dict(connection.execute("SELECT delivery_id, state FROM deliveries"))
        never_received, failed_at_sender = [], []
        for delivery in sender_deliveries:
            if delivery["id"] not in logged:
                (never_received if delivery.get("delivered") else failed_at_sender).append(delivery["id"])
        return {"never_received": sorted(never_received),       # request redelivery
                "failed_at_sender": sorted(failed_at_sender),   # the sender knows; request redelivery
                "unprocessed": sorted(i for i, state in logged.items() if state == "received")}
