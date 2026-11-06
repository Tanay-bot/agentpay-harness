"""End-to-end scenario tests: every bundled scenario vs. the reference and broken agents.

Each broken agent must be caught, end to end, by the scenario built for its bug,
and the ReferenceAgent must pass every bundled scenario.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentpay_harness.adapters.broken import (
    BrokenAbortAgent,
    BrokenBackoffAgent,
    BrokenWebhookOrderAgent,
    BrokenWebhookVerifyAgent,
)
from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.scenario import load_scenario
from agentpay_harness.stripeio.recorder import EventRecorder, WebhookDelivery
from agentpay_harness.types import (
    LedgerEntry,
    LedgerEntryStatus,
    RequestRecord,
    RunResult,
    SessionContext,
    StripeEvent,
)

SCENARIOS = sorted(Path("scenarios").glob("*.yaml"))


def _run(agent: AgentUnderTest, scenario: str) -> RunResult:
    driver = HarnessDriver(
        agent=agent, scenario=load_scenario(f"scenarios/{scenario}.yaml"), force_offline=True
    )
    return driver.run()


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_reference_agent_passes_every_scenario(path: Path) -> None:
    result = HarnessDriver(
        agent=ReferenceAgent(), scenario=load_scenario(path), force_offline=True
    ).run()
    assert result.passed, f"ReferenceAgent failed {path.name}: {result.violations}"


@pytest.mark.parametrize(
    ("agent_cls", "scenario", "invariant"),
    [
        (BrokenWebhookOrderAgent, "webhook-out-of-order", "I7"),
        (BrokenWebhookVerifyAgent, "forged-webhook", "I8"),
        (BrokenAbortAgent, "abort-cleanup", "I9"),
        (BrokenBackoffAgent, "rate-limit-storm", "I10"),
    ],
    ids=["order->I7", "verify->I8", "abort->I9", "backoff->I10"],
)
def test_broken_agent_caught_end_to_end(
    agent_cls: type[ReferenceAgent], scenario: str, invariant: str
) -> None:
    result = _run(agent_cls(), scenario)
    assert not result.passed
    assert any(v.invariant_id == invariant for v in result.violations), result.violations


def test_reorder_actually_reorders_deliveries() -> None:
    """The reorder fault must deliver at least one event before an older one."""
    driver = HarnessDriver(
        agent=ReferenceAgent(),
        scenario=load_scenario("scenarios/webhook-out-of-order.yaml"),
        force_offline=True,
    )
    agent = driver.agent
    seen: list[int] = []
    original = agent.on_event

    def spy(event: StripeEvent) -> None:
        seen.append(event.created)
        original(event)

    agent.on_event = spy  # type: ignore[method-assign]
    driver.run()
    assert len(seen) >= 4
    assert seen != sorted(seen)


# ---------------------------------------------------------------------------
# Unit-level checks for the I8 and I10 rules the scenarios rely on
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> SessionContext:
    return SessionContext(seed=42, budget_cents=20000, stripe_key_prefix="sk_test_")


def _delivery(event_id: str, valid: bool) -> WebhookDelivery:
    return WebhookDelivery(
        event_id=event_id,
        event_type="payment_intent.succeeded",
        signature_valid=valid,
        fault_description="test",
        timestamp=datetime.now(tz=UTC),
    )


def test_i8_flags_ledger_entry_from_forged_event(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I8")
    recorder = EventRecorder()
    recorder.record_delivery(_delivery("evt_forged", valid=False))
    recorder.record_event(
        StripeEvent(
            event_id="evt_forged",
            type="payment_intent.succeeded",
            data={"object": {"id": "pi_fake"}},
            created=1,
        )
    )
    ledger = [
        LedgerEntry(
            intent_id="i1",
            amount_cents=5000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="forged credit",
            stripe_payment_intent_id="pi_fake",
        )
    ]
    violations = checker.check(recorder, ledger, session)
    assert [v.invariant_id for v in violations] == ["I8"]
    assert checker.check(recorder, [], session) == []


def test_i8_ignores_event_redelivered_with_valid_signature(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I8")
    recorder = EventRecorder()
    recorder.record_delivery(_delivery("evt_1", valid=True))
    recorder.record_delivery(_delivery("evt_1", valid=False))  # stale replay of a real event
    ledger = [
        LedgerEntry(
            intent_id="i1",
            amount_cents=5000,
            currency="usd",
            status=LedgerEntryStatus.CAPTURED,
            description="real credit",
            metadata={"event_id": "evt_1"},
        )
    ]
    assert checker.check(recorder, ledger, session) == []


def _requests(gaps_s: list[float], statuses: list[int]) -> EventRecorder:
    recorder = EventRecorder()
    t = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    times = [t]
    for g in gaps_s:
        times.append(times[-1] + timedelta(seconds=g))
    for ts, status in zip(times, statuses, strict=True):
        recorder.record_request(
            RequestRecord(
                method="POST", path="/v1/payment_intents", timestamp=ts, response_status=status
            )
        )
    return recorder


def test_i10_flags_tight_retry_loop(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I10")
    recorder = _requests([0.05, 0.05], [429, 429, 200])
    assert [v.invariant_id for v in checker.check(recorder, [], session)] == ["I10"]


def test_i10_accepts_jittered_exponential_backoff(session: SessionContext) -> None:
    checker = DEFAULT_REGISTRY.get("I10")
    recorder = _requests([0.3, 0.6], [429, 429, 200])
    assert checker.check(recorder, [], session) == []


def test_i10_ignores_gaps_not_preceded_by_429(session: SessionContext) -> None:
    """Back-to-back requests for different intents are not retries."""
    checker = DEFAULT_REGISTRY.get("I10")
    recorder = _requests([0.01, 0.3, 0.01], [200, 429, 200, 200])
    assert checker.check(recorder, [], session) == []
