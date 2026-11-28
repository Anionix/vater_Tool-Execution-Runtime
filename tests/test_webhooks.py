"""task.optional_more_protocols.009 / 010 / 011: inbound event deliveries.

009: forged notifications (no, malformed or wrong signature, a tampered body) are always
refused and leave nothing behind. 010: duplicates are applied once, a reused id with another
body is refused, and every arrival order of a resource's events ends in the same state.
011: a delivery whose processing was interrupted is applied when redelivered, a resend of a
processed one is acknowledged without effect, and reconciliation names what never arrived.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import hmac
import inspect
from itertools import permutations
import json
import sqlite3
import threading

import pytest

from verified_execution import webhooks
from verified_execution.webhooks import WebhookReceiver, verify_signature

SECRET = b"webhook-secret-for-tests-0123456789"


def body_for(resource="issue-1", sequence=1, data=None) -> bytes:
    return json.dumps({"resource": resource, "sequence": sequence, "data": data or {"title": f"v{sequence}"}}).encode()


def headers_for(body: bytes, delivery="d-1", secret=SECRET, signature=None, event="issue.updated") -> dict:
    signed = signature if signature is not None else "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": signed, "X-Delivery-Id": delivery, "X-Event": event}


class Counting:
    """Each event carries the resource's current snapshot; the applied state is that snapshot."""

    def __init__(self, fail_first=False):
        self.calls, self.fail_first, self.lock = 0, fail_first, threading.Lock()

    def __call__(self, old, data):
        with self.lock:
            self.calls += 1
            if self.fail_first and self.calls == 1:
                raise RuntimeError("the process died while applying")
        return data


def rows(receiver) -> int:
    with closing(sqlite3.connect(receiver.path)) as connection:
        return connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]


@pytest.fixture
def receiver(tmp_path):
    apply = Counting()
    made = WebhookReceiver(tmp_path / "deliveries.sqlite3", SECRET, apply)
    made.apply_calls = apply
    return made


def test_a_signed_delivery_is_applied(receiver):
    body = body_for()
    assert receiver.receive(headers_for(body), body) == webhooks.Outcome(200, "applied")
    assert receiver.state("issue-1") == {"title": "v1"}


@pytest.mark.parametrize("forgery", ["missing", "other_algorithm", "wrong_secret", "tampered_body",
                                     "truncated", "uppercase", "empty", "not_hex"])
def test_a_forged_notification_is_always_refused_and_leaves_nothing(receiver, forgery):
    body = body_for()
    headers = headers_for(body)
    digest = headers["X-Hub-Signature-256"][len("sha256="):]
    if forgery == "missing":
        del headers["X-Hub-Signature-256"]
    elif forgery == "other_algorithm":
        headers["X-Hub-Signature-256"] = "sha1=" + hmac.new(SECRET, body, hashlib.sha1).hexdigest()
    elif forgery == "wrong_secret":
        headers = headers_for(body, secret=b"another-secret-of-the-same-size!!")
    elif forgery == "tampered_body":
        body = body_for(data={"title": "attacker"})
    elif forgery == "truncated":
        headers["X-Hub-Signature-256"] = "sha256=" + digest[:-2]
    elif forgery == "uppercase":
        headers["X-Hub-Signature-256"] = "sha256=" + digest.upper()
    elif forgery == "empty":
        headers["X-Hub-Signature-256"] = ""
    elif forgery == "not_hex":
        headers["X-Hub-Signature-256"] = "sha256=" + "z" * 64
    assert receiver.receive(headers, body) == webhooks.Outcome(401, "rejected_signature")
    assert rows(receiver) == 0 and receiver.state("issue-1") is None and receiver.apply_calls.calls == 0


def test_signatures_are_compared_in_constant_time_and_need_a_real_secret(tmp_path):
    source = inspect.getsource(verify_signature)
    assert "hmac.compare_digest" in source and "== match" not in source
    assert verify_signature(b"", b"{}", "sha256=" + hmac.new(b"", b"{}", hashlib.sha256).hexdigest()) is False
    for secret in (b"", b"short", "a-text-secret-not-bytes-0123456789"):
        with pytest.raises(ValueError):
            WebhookReceiver(tmp_path / "x.sqlite3", secret, lambda old, data: data)


@pytest.mark.parametrize("problem", ["no_delivery_id", "bad_json", "negative_sequence", "no_resource"])
def test_an_authentic_but_malformed_delivery_is_refused(receiver, problem):
    body = {"bad_json": b"{not json", "negative_sequence": json.dumps({"resource": "r", "sequence": -1}).encode(),
            "no_resource": json.dumps({"sequence": 1}).encode()}.get(problem, body_for())
    headers = headers_for(body)
    if problem == "no_delivery_id":
        del headers["X-Delivery-Id"]
    assert receiver.receive(headers, body) == webhooks.Outcome(400, "malformed")
    assert rows(receiver) == 0


def test_a_duplicate_delivery_is_applied_once(receiver):
    body = body_for()
    assert receiver.receive(headers_for(body), body).kind == "applied"
    assert receiver.receive(headers_for(body), body) == webhooks.Outcome(200, "duplicate")
    assert receiver.apply_calls.calls == 1


def test_a_reused_delivery_id_with_another_body_is_refused(receiver):
    first, second = body_for(sequence=1), body_for(sequence=2, data={"title": "replaced"})
    receiver.receive(headers_for(first), first)
    assert receiver.receive(headers_for(second), second) == webhooks.Outcome(409, "conflict")
    assert receiver.state("issue-1") == {"title": "v1"}


@pytest.mark.parametrize("order", list(permutations([1, 2, 3])), ids=lambda order: "-".join(map(str, order)))
def test_every_arrival_order_ends_in_the_newest_state(tmp_path, order):
    receiver = WebhookReceiver(tmp_path / "d.sqlite3", SECRET, Counting())
    kinds = []
    for sequence in order:
        body = body_for(sequence=sequence)
        kinds.append(receiver.receive(headers_for(body, delivery=f"d-{sequence}"), body).kind)
    assert receiver.state("issue-1") == {"title": "v3"}
    assert kinds.count("applied") + kinds.count("stale") == 3
    assert kinds[order.index(3)] == "applied"  # the newest is always applied; anything after it is stale


def test_concurrent_duplicates_apply_once(receiver):
    body = body_for()
    with ThreadPoolExecutor(8) as pool:
        kinds = list(pool.map(lambda _: receiver.receive(headers_for(body), body).kind, range(8)))
    assert sorted(kinds) == ["applied"] + ["duplicate"] * 7
    assert receiver.apply_calls.calls == 1


def test_an_interrupted_delivery_is_applied_on_redelivery_and_a_resend_is_not(tmp_path):
    apply = Counting(fail_first=True)
    receiver = WebhookReceiver(tmp_path / "d.sqlite3", SECRET, apply)
    body = body_for()
    assert receiver.receive(headers_for(body), body) == webhooks.Outcome(500, "failed")
    assert receiver.state("issue-1") is None  # no partial effect
    assert receiver.reconcile([]) == {"never_received": [], "failed_at_sender": [], "unprocessed": ["d-1"]}
    assert receiver.receive(headers_for(body), body).kind == "applied"  # its result had been lost
    assert receiver.receive(headers_for(body), body).kind == "duplicate"  # a resend: no effect
    assert apply.calls == 2 and receiver.state("issue-1") == {"title": "v1"}
    assert receiver.reconcile([])["unprocessed"] == []


def test_reconciliation_names_deliveries_that_never_arrived(receiver):
    for sequence in (1, 2):
        body = body_for(sequence=sequence)
        receiver.receive(headers_for(body, delivery=f"d-{sequence}"), body)
    sender = [{"id": "d-1", "delivered": True}, {"id": "d-2", "delivered": True},
              {"id": "d-3", "delivered": True}, {"id": "d-4", "delivered": False}]
    assert receiver.reconcile(sender) == {"never_received": ["d-3"], "failed_at_sender": ["d-4"], "unprocessed": []}
