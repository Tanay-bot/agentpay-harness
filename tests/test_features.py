"""Clock skew, partial capture, refunds, multi-agent races, USD-only, and the Stripe CLI mode."""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal

import pytest

from agentpay_harness.adapters.broken import (
    BrokenBudgetAgent,
    BrokenCaptureAgent,
    BrokenRefundAgent,
    BrokenWebhookVerifyAgent,
)
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.budget import InterleavingScheduler, SharedBudget
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.scenario import load_scenario
from agentpay_harness.stripecli import (
    StripeCLIError,
    StripeCLISession,
    find_stripe_cli,
    require_test_key,
)
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    FaultConfig,
    FaultType,
    Intent,
    RunResult,
    ScenarioConfig,
    StripeEvent,
    Task,
    UnsupportedCurrencyError,
)
from agentpay_harness.webhooks.receiver import WebhookDeliverySystem
from agentpay_harness.webhooks.signatures import generate_test_signature


def _run(agent: ReferenceAgent, name: str) -> RunResult:
    return HarnessDriver(
        agent=agent, scenario=load_scenario(f"scenarios/{name}.yaml"), force_offline=True
    ).run()


@pytest.mark.parametrize(
    ("agent_cls", "scenario", "invariant"),
    [
        (BrokenWebhookVerifyAgent, "clock-skew", "I8"),
        (BrokenCaptureAgent, "partial-capture", "I12"),
        (BrokenRefundAgent, "refund-overflow", "I13"),
        (BrokenBudgetAgent, "budget-race", "I2"),
    ],
    ids=["skew->I8", "capture->I12", "refund->I13", "race->I2"],
)
def test_broken_agent_caught(
    agent_cls: type[ReferenceAgent], scenario: str, invariant: str
) -> None:
    result = _run(agent_cls(), scenario)
    assert any(v.invariant_id == invariant for v in result.violations), result.violations


# --- clock skew -----------------------------------------------------------


def test_clock_skew_only_invalidates_signatures_outside_tolerance() -> None:
    recorder = EventRecorder()
    system = WebhookDeliverySystem(
        target_callback=lambda e: None,
        recorder=recorder,
        clock_skews=[("payment_intent.created", -120), ("payment_intent.succeeded", 900)],
    )
    for i, evt_type in enumerate(["payment_intent.created", "payment_intent.succeeded"]):
        system.enqueue(StripeEvent(event_id=f"evt_{i}", type=evt_type, data={}, created=i + 1))
    system.flush()
    verdicts = {d.event_type: d.signature_valid for d in recorder.deliveries}
    assert verdicts == {"payment_intent.created": True, "payment_intent.succeeded": False}


# --- partial capture & refunds -------------------------------------------


def test_reference_books_captured_amount_and_refunds_only_that() -> None:
    agent = ReferenceAgent()
    result = _run(agent, "refund-overflow")
    assert result.passed, result.violations
    [entry] = agent.ledger()
    assert entry.amount_cents == 3000  # 60% of the $50 authorization
    assert entry.metadata["authorized_cents"] == 5000
    assert entry.metadata["refunded_cents"] == 3000
    refunds = [r for r in result.events if r.path == "/v1/refunds"]
    assert [r.body["amount"] for r in refunds if r.body] == [3000]


def test_mock_rejects_refund_larger_than_capture() -> None:
    result = _run(BrokenRefundAgent(), "refund-overflow")
    [refund] = [r for r in result.events if r.path == "/v1/refunds"]
    assert refund.response_status == 400
    assert refund.response_body and refund.response_body["error"]["code"] == "amount_too_large"


# --- multi-agent race -----------------------------------------------------


def _race(schedule: list[int]) -> RunResult:
    scenario = load_scenario("scenarios/budget-race.yaml")
    faults = [FaultConfig(type=FaultType.INTERLEAVE, schedule=schedule)]
    return HarnessDriver(
        agent=BrokenBudgetAgent(), scenario=replace(scenario, faults=faults), force_offline=True
    ).run()


def test_interleaving_schedule_decides_the_race() -> None:
    """Same racy agent: interleaved reads overspend, serialized reads do not."""
    assert any(v.invariant_id == "I2" for v in _race([0, 1, 0, 1]).violations)
    assert _race([0, 0, 1, 1]).passed


def test_reference_agents_never_overspend_shared_budget() -> None:
    for schedule in ([0, 1, 0, 1], [1, 0, 1, 0], [0, 0, 1, 1]):
        scenario = load_scenario("scenarios/budget-race.yaml")
        faults = [FaultConfig(type=FaultType.INTERLEAVE, schedule=schedule)]
        driver = HarnessDriver(
            agent=ReferenceAgent(), scenario=replace(scenario, faults=faults), force_offline=True
        )
        result = driver.run()
        assert result.passed, (schedule, result.violations)
        assert sum(len(a.ledger()) for a in driver.agents) == 1


def test_scheduler_follows_schedule_and_skips_finished_agents() -> None:
    import threading

    scheduler = InterleavingScheduler([1, 0, 1, 0, 0], n_agents=2)
    budget = SharedBudget(10_000, scheduler)

    def agent(index: int, ops: int) -> None:
        scheduler.bind(index)
        for _ in range(ops):
            budget.read()
        scheduler.finish(index)

    threads = [
        threading.Thread(target=agent, args=(0, 3)),
        threading.Thread(target=agent, args=(1, 1)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert scheduler.trace == [1, 0, 0, 0]


def test_seeded_schedule_is_reproducible() -> None:
    a = InterleavingScheduler.seeded(7, 2).schedule
    assert a == InterleavingScheduler.seeded(7, 2).schedule
    assert sorted(a) == [0, 0, 0, 0, 1, 1, 1, 1]


# --- USD only -------------------------------------------------------------


class _EuroAgent(ReferenceAgent):
    def plan(self, task: Task) -> list[Intent]:
        return [replace(i, currency="eur") for i in super().plan(task)]


def test_driver_rejects_non_usd_intents_loudly() -> None:
    with pytest.raises(UnsupportedCurrencyError, match="USD-only"):
        _run(_EuroAgent(), "webhook-chaos")


def test_scenario_config_rejects_non_usd() -> None:
    with pytest.raises(UnsupportedCurrencyError):
        ScenarioConfig(name="x", budget_usd=Decimal(1), task="t", seed=1, currency="eur")


# --- Stripe CLI mode (offline parts) -------------------------------------


def test_stripe_cli_mode_refuses_mock_keys() -> None:
    with pytest.raises(StripeCLIError, match="real Stripe test-mode key"):
        require_test_key("sk_test_agentpay_mock_key_default")


def test_stripe_cli_mode_explains_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentpay_harness.stripecli.find_stripe_cli", lambda: None)
    with pytest.raises(StripeCLIError, match="not on PATH"):
        StripeCLISession("sk_test_realistic_key_123", "http://127.0.0.1:1/webhook")


def test_genuine_delivery_is_verified_with_the_cli_secret() -> None:
    """Real Stripe bodies + headers pass through untouched and verify with the CLI secret."""
    secret = "whsec_test_from_stripe_cli"
    body = b'{"id": "evt_real", "type": "payment_intent.succeeded", "data": {"object": {}}}'
    header = generate_test_signature(body, secret)
    agent = ReferenceAgent(webhook_secret=secret)
    recorder = EventRecorder()
    system = WebhookDeliverySystem(agent.on_event, recorder=recorder, signing_secret=secret)
    event = StripeEvent(event_id="evt_real", type="payment_intent.succeeded", data={}, created=1)
    system.deliver_genuine(event, body, header)
    assert [d.signature_valid for d in recorder.deliveries] == [True]
    assert "evt_real" in agent._seen_event_ids


# --- Stripe CLI mode (real Stripe) ---------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(find_stripe_cli() is None, reason="Stripe CLI not installed")
@pytest.mark.skipif(
    not os.environ.get("STRIPE_TEST_SECRET_KEY", "").startswith(("sk_test_", "rk_test_")),
    reason="STRIPE_TEST_SECRET_KEY not set",
)
def test_genuine_stripe_events_end_to_end() -> None:
    agent = ReferenceAgent()
    driver = HarnessDriver(
        agent=agent,
        scenario=load_scenario("scenarios/integration/stripe-cli-webhooks.yaml"),
        stripe_key=os.environ["STRIPE_TEST_SECRET_KEY"],
        force_offline=False,
    )
    result = driver.run()
    assert result.passed, result.violations
    # The agent accepted at least one genuine, Stripe-signed event.
    assert any(e.startswith("evt_") and "forged" not in e for e in agent._seen_event_ids)


def test_recorder_json_round_trip() -> None:
    """to_json/from_json used field names that don't exist; this keeps it honest."""
    from agentpay_harness.types import RequestRecord

    recorder = EventRecorder()
    recorder.record_request(
        RequestRecord(method="POST", path="/v1/refunds", body={"amount": 5}, response_status=200)
    )
    recorder.record_event(
        StripeEvent(event_id="evt_1", type="payment_intent.succeeded", data={"x": 1}, created=7)
    )
    restored = EventRecorder.from_json(recorder.to_json())
    assert restored.requests[0].body == {"amount": 5}
    assert restored.events[0].event_id == "evt_1"
    assert restored.events[0].created == 7
