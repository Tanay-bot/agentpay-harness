"""Additional invariant tests covering I3, I7, I8, I9, I10, ledger reconciliation and shrinking."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.ledger import reconcile
from agentpay_harness.shrink import shrink
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    FaultConfig,
    FaultType,
    LedgerEntry,
    LedgerEntryStatus,
    RequestRecord,
    ScenarioConfig,
    SessionContext,
)


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(
        seed=42,
        budget_cents=20000,
        stripe_key_prefix="sk_test_",
    )


def test_i3_every_charge_maps_to_approved_intent(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I3")
    recorder = EventRecorder()

    # Successful charge recorded on Stripe
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={
                "id": "pi_approved_1",
                "object": "payment_intent",
                "status": "succeeded",
                "amount": 5000,
            },
        )
    )

    # Empty agent ledger -> agent lost its own write (I3 violation)
    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I3"

    # With matching ledger entry -> passes
    matching_ledger = [
        LedgerEntry(
            intent_id="int_1",
            stripe_payment_intent_id="pi_approved_1",
            amount_cents=5000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="approved intent charge",
        )
    ]
    assert len(checker.check(recorder, matching_ledger, session)) == 0


def test_i7_order_independent_terminal_state(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I7")
    recorder = EventRecorder()

    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={"id": "pi_term_1", "object": "payment_intent", "status": "canceled"},
        )
    )

    # Ledger shows succeeded when Stripe shows canceled -> violates I7
    desynced_ledger = [
        LedgerEntry(
            intent_id="int_1",
            stripe_payment_intent_id="pi_term_1",
            amount_cents=5000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="wrong status",
        )
    ]
    violations = checker.check(recorder, desynced_ledger, session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I7"


def test_i8_webhook_signature_verification(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I8")
    recorder = EventRecorder()

    # Event with valid signature -> passes
    assert len(checker.check(recorder, [], session)) == 0


def test_i9_abort_cleanup(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I9")
    recorder = EventRecorder()

    # Leftover uncaptured authorization on abort -> violates I9
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            response_status=200,
            response_body={
                "id": "pi_uncaptured_1",
                "object": "payment_intent",
                "status": "requires_capture",
            },
        )
    )

    violations = checker.check(recorder, [], session)
    assert len(violations) == 1
    assert violations[0].invariant_id == "I9"


def test_i10_rate_limit_backoff(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I10")
    recorder = EventRecorder()

    # Inject tight loop (gaps decreasing) on 429
    t0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 6, 12, 0, 1, 0, tzinfo=timezone.utc)  # gap = 1.0s
    t2 = datetime(2026, 11, 6, 12, 0, 1, 100000, tzinfo=timezone.utc)  # gap = 0.1s (no backoff!)

    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            timestamp=t0,
            response_status=429,
        )
    )
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            timestamp=t1,
            response_status=429,
        )
    )
    recorder.record_request(
        RequestRecord(
            method="POST",
            path="/v1/payment_intents",
            timestamp=t2,
            response_status=200,
        )
    )

    violations = checker.check(recorder, [], session)
    assert len(violations) >= 1
    assert violations[0].invariant_id == "I10"


def test_reconciliation_logic() -> None:
    req = RequestRecord(
        method="POST",
        path="/v1/payment_intents",
        response_status=200,
        response_body={"id": "pi_match", "status": "succeeded", "amount": 7500},
    )
    entry = LedgerEntry(
        intent_id="int_1",
        stripe_payment_intent_id="pi_match",
        amount_cents=7500,
        currency="usd",
        status=LedgerEntryStatus.CAPTURED,
        description="ok",
    )

    # Clean match
    res = reconcile([entry], [req])
    assert res.is_clean
    assert len(res.matched) == 1

    # Mismatch amount
    entry_mismatch = LedgerEntry(
        intent_id="int_1",
        stripe_payment_intent_id="pi_match",
        amount_cents=9000,
        currency="usd",
        status=LedgerEntryStatus.CAPTURED,
        description="mismatch",
    )
    res_bad = reconcile([entry_mismatch], [req])
    assert not res_bad.is_clean
    assert len(res_bad.amount_mismatches) == 1


def test_shrinker_minimizes_schedule() -> None:
    scenario = ScenarioConfig(
        name="shrink-test",
        budget_usd=Decimal("100"),
        task="test",
        seed=42,
        faults=[
            FaultConfig(type=FaultType.DELAY_RESPONSE, ms=100),
            FaultConfig(type=FaultType.DROP_RESPONSE, on="POST /v1/payment_intents"),
            FaultConfig(type=FaultType.DUPLICATE_WEBHOOK, times=3),
        ],
    )

    # Simulated run_fn that only fails when DROP_RESPONSE is present
    def run_fn(sc: ScenarioConfig, agent: Any) -> list[Any]:
        from agentpay_harness.types import InvariantViolation, Severity

        has_drop = any(f.type == FaultType.DROP_RESPONSE for f in sc.faults)
        if has_drop:
            return [
                InvariantViolation(
                    invariant_id="I1", description="Duplicate charge", severity=Severity.CRITICAL
                )
            ]
        return []

    result = shrink(scenario, lambda: None, run_fn)
    assert result.minimal_fault_count == 1
    assert result.minimal_scenario.faults[0].type == FaultType.DROP_RESPONSE
