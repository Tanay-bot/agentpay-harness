"""The shrinker must actually minimize: a noisy failing schedule shrinks to its real cause."""

from __future__ import annotations

from decimal import Decimal

from agentpay_harness.adapters.broken import BrokenIdempotencyAgent
from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.shrink import shrink
from agentpay_harness.types import (
    FaultConfig,
    FaultType,
    InvariantViolation,
    ScenarioConfig,
    Severity,
)

DROP = FaultConfig(type=FaultType.DROP_RESPONSE, on="POST /v1/payment_intents", occurrence=1)

#: Six faults; only the dropped response makes BrokenIdempotencyAgent double-charge.
SIX_FAULTS = [
    FaultConfig(type=FaultType.DUPLICATE_WEBHOOK, event="payment_intent.succeeded", times=2),
    FaultConfig(type=FaultType.REORDER_WEBHOOKS, window=3),
    DROP,
    FaultConfig(type=FaultType.REPLAY_WEBHOOK, event="payment_intent.created", delay_s=30),
    FaultConfig(type=FaultType.RATE_LIMIT, on="POST /v1/payment_intents", occurrence=3),
    FaultConfig(type=FaultType.DELAY_RESPONSE, on="POST /v1/refunds", ms=10),
]


def _scenario(faults: list[FaultConfig]) -> ScenarioConfig:
    return ScenarioConfig(
        name="noisy-duplicate",
        budget_usd=Decimal("100"),
        task="buy one API credit pack at $50",
        seed=42,
        faults=faults,
        assertions=["I1"],
    )


def _run(sc: ScenarioConfig, agent: AgentUnderTest) -> list[InvariantViolation]:
    return HarnessDriver(agent=agent, scenario=sc, force_offline=True).run().violations


def test_six_fault_schedule_shrinks_to_the_dropped_response() -> None:
    scenario = _scenario(SIX_FAULTS)
    assert any(v.invariant_id == "I1" for v in _run(scenario, BrokenIdempotencyAgent()))

    result = shrink(scenario, BrokenIdempotencyAgent, _run, target_invariant_id="I1")

    assert result.original_fault_count == 6
    assert result.minimal_fault_count == 1
    assert result.minimal_scenario.faults == [DROP]
    # The minimal schedule still reproduces the bug on its own.
    assert any(
        v.invariant_id == "I1" for v in _run(result.minimal_scenario, BrokenIdempotencyAgent())
    )


def test_shrinker_keeps_every_fault_a_failure_needs() -> None:
    """When the bug needs two faults together, both survive (1-minimality)."""
    needed = {FaultType.DROP_RESPONSE, FaultType.RATE_LIMIT}
    faults = [
        FaultConfig(type=t, on="POST /v1/payment_intents")
        for t in (
            FaultType.SERVER_ERROR,
            FaultType.DROP_RESPONSE,
            FaultType.DELAY_RESPONSE,
            FaultType.RATE_LIMIT,
            FaultType.DUPLICATE_REQUEST,
        )
    ]

    def run_fn(sc: ScenarioConfig, agent: AgentUnderTest) -> list[InvariantViolation]:
        if needed <= {f.type for f in sc.faults}:
            return [InvariantViolation("I1", "needs both", Severity.CRITICAL)]
        return []

    result = shrink(_scenario(faults), BrokenIdempotencyAgent, run_fn)
    assert {f.type for f in result.minimal_scenario.faults} == needed


def test_shrinker_preserves_scenario_settings() -> None:
    scenario = ScenarioConfig(
        name="keep",
        budget_usd=Decimal("100"),
        task="reserve two seats at $40 each",
        seed=3,
        faults=SIX_FAULTS,
        capture_method="manual",
        abort=True,
    )
    result = shrink(
        scenario,
        BrokenIdempotencyAgent,
        lambda sc, ag: [InvariantViolation("I9", "x", Severity.ERROR)],
    )
    assert result.minimal_scenario.capture_method == "manual"
    assert result.minimal_scenario.abort is True
    # The failure needs no fault at all, so every fault is shrunk away.
    assert result.minimal_fault_count == 0
