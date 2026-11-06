"""The scenario driver — executes an agent through a purchase loop under adversarial conditions.

Applies fault schedules deterministically, delivers chaotic webhooks, records all
interactions, and evaluates payment invariants.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.faults.engine import FaultEngine
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.safety import load_stripe_key_from_env, validate_stripe_key
from agentpay_harness.stripeio.client import RecordingStripeClient
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    Intent,
    PaymentResult,
    PaymentStatus,
    RunResult,
    ScenarioConfig,
    SessionContext,
    StripeEvent,
    Task,
)
from agentpay_harness.webhooks.receiver import WebhookDeliverySystem

logger = logging.getLogger(__name__)


class HarnessDriver:
    """Runs a ScenarioConfig against an AgentUnderTest.

    Coordinates fault injection, agent payments, simulated webhooks, and invariant checks.
    """

    def __init__(
        self,
        agent: AgentUnderTest,
        scenario: ScenarioConfig,
        stripe_key: str | None = None,
        stripe_base_url: str | None = None,
        force_offline: bool = True,
    ) -> None:
        self.agent = agent
        self.scenario = scenario
        self.stripe_key = stripe_key or load_stripe_key_from_env()
        validate_stripe_key(self.stripe_key)
        self.stripe_base_url = stripe_base_url
        self.force_offline = force_offline

    def run(self) -> RunResult:
        """Execute the scenario and evaluate all asserted invariants."""
        t_start = time.perf_counter()

        session = SessionContext(
            seed=self.scenario.seed,
            budget_cents=self.scenario.budget_cents,
            stripe_key_prefix=self.stripe_key[:8],
            start_time=datetime.now(tz=UTC),
        )

        recorder = EventRecorder()
        fault_engine = FaultEngine(self.scenario.faults, seed=self.scenario.seed)

        # Build recording client with fault interceptor
        client = RecordingStripeClient(
            api_key=self.stripe_key,
            base_url=self.stripe_base_url,
            fault_interceptor=fault_engine.intercept_request,
            force_offline=self.force_offline,
        )

        # Setup webhook delivery system
        webhook_delivery = WebhookDeliverySystem(
            target_callback=self.agent.on_event,
            fault_engine=fault_engine,
            recorder=recorder,
        )

        # 1. Step 1: Agent plans the task
        task = Task(
            description=self.scenario.task,
            budget_cents=self.scenario.budget_cents,
        )
        intents = self.agent.plan(task)

        # 2. Step 2: Execute payments (with concurrency if configured)
        payment_results: list[PaymentResult] = []

        def _execute_intent(intent: Intent) -> PaymentResult:
            res = self.agent.pay(intent, client)
            # Synthesize and deliver webhook for any created/captured payment intent
            if res.stripe_payment_intent_id:
                evt_type = (
                    "payment_intent.succeeded"
                    if res.status == PaymentStatus.SUCCEEDED
                    else "payment_intent.created"
                )
                event = StripeEvent(
                    event_id=f"evt_{uuid4().hex[:16]}",
                    type=evt_type,
                    data={
                        "object": {
                            "id": res.stripe_payment_intent_id,
                            "amount": res.amount_cents,
                            "currency": "usd",
                            "status": "succeeded"
                            if res.status == PaymentStatus.SUCCEEDED
                            else "requires_payment_method",
                            "metadata": {"intent_id": intent.intent_id},
                        }
                    },
                    created=int(time.time()),
                    livemode=False,
                )
                webhook_delivery.deliver(event)
            return res

        if self.scenario.concurrency > 1 and len(intents) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.scenario.concurrency
            ) as executor:
                futures = [executor.submit(_execute_intent, intent) for intent in intents]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        payment_results.append(future.result())
                    except Exception as exc:
                        logger.exception("Error executing intent in thread pool: %s", exc)
        else:
            for intent in intents:
                payment_results.append(_execute_intent(intent))

        # Copy client request records into the EventRecorder
        for req in client.get_records():
            recorder.record_request(req)

        # 3. Step 3: Evaluate Invariants
        assertions = self.scenario.assertions or list(DEFAULT_REGISTRY._checkers.keys())
        violations = DEFAULT_REGISTRY.check_all(
            ids=assertions,
            recorder=recorder,
            ledger=self.agent.ledger(),
            session=session,
        )

        t_end = time.perf_counter()
        duration = t_end - t_start

        return RunResult(
            session=session,
            events=client.get_records(),
            violations=violations,
            passed=len(violations) == 0,
            duration_seconds=duration,
        )
