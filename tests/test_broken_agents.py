"""Tests for the tester: Verify that every broken reference agent is caught by the harness.

This mirrors the lookahead test in backtest-core and is the centerpiece of the harness's
credibility: every invariant must catch its corresponding deliberately broken agent.
"""

from __future__ import annotations

from decimal import Decimal

from agentpay_harness.adapters.broken import (
    BrokenBudgetAgent,
    BrokenIdempotencyAgent,
    BrokenLedgerAgent,
    BrokenWebhookDedupeAgent,
)
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.scenario import load_scenario
from agentpay_harness.types import ScenarioConfig


def test_reference_agent_passes_duplicate_timeout_scenario() -> None:
    """The correct ReferenceAgent must pass the duplicate-on-timeout scenario."""
    scenario = load_scenario("scenarios/duplicate-on-timeout.yaml")
    driver = HarnessDriver(agent=ReferenceAgent(), scenario=scenario, force_offline=True)
    result = driver.run()
    assert result.passed, f"ReferenceAgent unexpectedly failed: {result.violations}"


def test_broken_idempotency_agent_caught_by_i1() -> None:
    """BrokenIdempotencyAgent must be caught by I1 (duplicate charge on retry)."""
    scenario = load_scenario("scenarios/duplicate-on-timeout.yaml")
    driver = HarnessDriver(agent=BrokenIdempotencyAgent(), scenario=scenario, force_offline=True)
    result = driver.run()
    assert not result.passed
    assert any(v.invariant_id == "I1" for v in result.violations)


def test_broken_budget_agent_caught_by_i2() -> None:
    """BrokenBudgetAgent must be caught by I2 (concurrency overspend)."""
    scenario = load_scenario("scenarios/runaway-spend.yaml")
    driver = HarnessDriver(agent=BrokenBudgetAgent(), scenario=scenario, force_offline=True)
    result = driver.run()
    # BrokenBudgetAgent does not synchronize budget check across concurrent tasks
    assert any(v.invariant_id in ("I2", "I11") for v in result.violations)


def test_broken_webhook_dedupe_agent_caught_by_i6() -> None:
    """BrokenWebhookDedupeAgent must be caught by I6 when duplicate webhooks arrive."""
    scenario = load_scenario("scenarios/webhook-chaos.yaml")
    driver = HarnessDriver(agent=BrokenWebhookDedupeAgent(), scenario=scenario, force_offline=True)
    result = driver.run()
    assert any(v.invariant_id == "I6" for v in result.violations)


def test_broken_ledger_agent_caught_by_i11() -> None:
    """BrokenLedgerAgent must be caught by I11 (ledger does not match Stripe)."""
    scenario = ScenarioConfig(
        name="test-ledger-sync",
        budget_usd=Decimal("100"),
        task="buy credits",
        seed=42,
        assertions=["I11"],
    )
    driver = HarnessDriver(agent=BrokenLedgerAgent(), scenario=scenario, force_offline=True)
    result = driver.run()
    assert any(v.invariant_id == "I11" for v in result.violations)
