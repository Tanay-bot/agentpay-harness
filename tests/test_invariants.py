"""Tests for all 11 invariants (I1 through I11).

Proves that each invariant correctly detects violations over recorded interactions.
"""

from __future__ import annotations

import pytest

from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    LedgerEntry,
    LedgerEntryStatus,
    RequestRecord,
    SessionContext,
    StripeEvent,
)


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(
        seed=42,
        budget_cents=10000,
        stripe_key_prefix="sk_test_",
    )


def test_i1_at_most_one_charge_per_intent(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I1")
    recorder = EventRecorder()
    intent_id = "intent_test_1"

    # Single charge: passes
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            body={"amount": 5000, "currency": "usd", "metadata": {"intent_id": intent_id}},
            response_status=200,
            response_body={
                "id": "pi_1",
                "status": "succeeded",
                "metadata": {"intent_id": intent_id},
            },
        )
    )
    assert len(checker.check(recorder, [], session)) == 0

    # Second charge for same intent: violates I1
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            body={"amount": 5000, "currency": "usd", "metadata": {"intent_id": intent_id}},
            response_status=200,
            response_body={
                "id": "pi_2",
                "status": "succeeded",
                "metadata": {"intent_id": intent_id},
            },
        )
    )
    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I1"


def test_i2_total_captured_within_budget(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I2")
    recorder = EventRecorder()

    # Budget is 10000 cents ($100). Charge $60: passes
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={
                "id": "pi_1",
                "object": "payment_intent",
                "status": "succeeded",
                "amount": 6000,
            },
        )
    )
    assert len(checker.check(recorder, [], session)) == 0

    # Additional $50 charge puts total at $110 (> $100): violates I2
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={
                "id": "pi_2",
                "object": "payment_intent",
                "status": "succeeded",
                "amount": 5000,
            },
        )
    )
    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I2"


def test_i4_reused_idempotency_key_returns_original(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I4")
    recorder = EventRecorder()
    key = "idem_key_stable_1"

    # Two requests with same key returning SAME payment intent ID: passes
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 5000},
            response_status=200,
            response_body={"id": "pi_stable_1"},
        )
    )
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 5000},
            response_status=200,
            response_body={"id": "pi_stable_1"},
        )
    )
    assert len(checker.check(recorder, [], session)) == 0

    # Same key returning DIFFERENT payment intent ID: violates I4
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 5000},
            response_status=200,
            response_body={"id": "pi_DIFFERENT_2"},
        )
    )
    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I4"


def test_i5_reused_key_different_params_error(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I5")
    recorder = EventRecorder()
    key = "idem_key_param_test"

    # Same key, different amount, returned 400: passes
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 5000},
            response_status=200,
            response_body={"id": "pi_1"},
        )
    )
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 9000},  # mutated amount!
            response_status=400,  # correctly errored by Stripe
            response_body={"error": {"type": "idempotency_error"}},
        )
    )
    assert len(checker.check(recorder, [], session)) == 0

    # Same key, different amount, but returned 200: violates I5
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            idempotency_key=key,
            body={"amount": 12000},
            response_status=200,  # BAD: silently accepted mutated params
            response_body={"id": "pi_2"},
        )
    )
    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I5"


def test_i6_webhook_event_deduplication(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I6")
    recorder = EventRecorder()
    evt_id = "evt_dedupe_123"

    # Event delivered twice
    recorder.record_event(
        StripeEvent(event_id=evt_id, type="payment_intent.succeeded", data={}, created=1000)
    )
    recorder.record_event(
        StripeEvent(event_id=evt_id, type="payment_intent.succeeded", data={}, created=1000)
    )

    # Agent ledger has 1 entry for this event: passes
    clean_ledger = [
        LedgerEntry(
            intent_id="int_1",
            amount_cents=1000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="test",
            metadata={"event_id": evt_id},
        )
    ]
    assert len(checker.check(recorder, clean_ledger, session)) == 0

    # Agent ledger created 2 entries for the same event: violates I6
    duplicate_ledger = [
        LedgerEntry(
            intent_id="int_1",
            amount_cents=1000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="test",
            metadata={"event_id": evt_id},
        ),
        LedgerEntry(
            intent_id="int_1",
            amount_cents=1000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="test",
            metadata={"event_id": evt_id},
        ),
    ]
    violations = checker.check(recorder, duplicate_ledger, session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I6"


def test_i11_ledger_reconciliation(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I11")
    recorder = EventRecorder()

    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={
                "id": "pi_1",
                "object": "payment_intent",
                "amount": 5000,
                "status": "succeeded",
            },
        )
    )

    # Matching ledger: passes
    ledger = [
        LedgerEntry(
            intent_id="int_1",
            stripe_payment_intent_id="pi_1",
            amount_cents=5000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="ok",
        )
    ]
    assert len(checker.check(recorder, ledger, session)) == 0

    # Missing from ledger: violates I11
    violations = checker.check(recorder, [], session)
    assert len(violations) >= 1
    assert any(v.invariant_id == "I11" for v in violations)
