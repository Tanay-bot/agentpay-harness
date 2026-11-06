"""The scenario driver — executes an agent through a purchase loop under adversarial conditions.

Applies fault schedules deterministically, delivers chaotic webhooks, records all
interactions, and evaluates payment invariants.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.faults.engine import FaultEngine
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.safety import load_stripe_key_from_env, validate_stripe_key
from agentpay_harness.stripeio.client import RecordingStripeClient
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    FaultType,
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

        # Webhooks are queued while the agent pays and delivered afterwards,
        # like Stripe's asynchronous delivery.  Reordering is a queue-level fault.
        reorder_window = max(
            (f.window or 3 for f in self.scenario.faults if f.type is FaultType.REORDER_WEBHOOKS),
            default=0,
        )
        webhook_delivery = WebhookDeliverySystem(
            target_callback=self.agent.on_event,
            fault_engine=fault_engine,
            recorder=recorder,
            reorder_window=reorder_window,
            seed=self.scenario.seed,
        )

        # Logical clock: every event gets a distinct, increasing `created` second.
        clock_lock = threading.Lock()
        clock = {"next": int(time.time())}

        def _tick() -> int:
            with clock_lock:
                clock["next"] += 1
                return clock["next"]

        def _event(evt_type: str, pi: dict[str, Any], intent_id: str) -> StripeEvent:
            return StripeEvent(
                event_id=f"evt_{uuid4().hex[:16]}",
                type=evt_type,
                data={
                    "object": {
                        "id": pi["id"],
                        "object": "payment_intent",
                        "amount": pi.get("amount"),
                        "currency": pi.get("currency", "usd"),
                        "status": pi.get("status"),
                        "metadata": {"intent_id": intent_id},
                    }
                },
                created=_tick(),
                livemode=False,
            )

        # 1. Step 1: Agent plans the task
        task = Task(
            description=self.scenario.task,
            budget_cents=self.scenario.budget_cents,
            metadata={"capture_method": self.scenario.capture_method},
        )
        intents = self.agent.plan(task)
        if self.scenario.capture_method != "automatic":
            intents = [replace(i, capture_method=self.scenario.capture_method) for i in intents]

        # 2. Step 2: Execute payments (with concurrency if configured)
        payment_results: list[PaymentResult] = []
        async_settled: list[str] = []
        pending_async: set[str] = set()

        def client_async_failures() -> set[str]:
            with clock_lock:
                pending_async.update(client.take_async_failures())
                return set(pending_async)

        def _execute_intent(intent: Intent) -> PaymentResult:
            res = self.agent.pay(intent, client)
            if not res.stripe_payment_intent_id:
                return res
            pi_id = res.stripe_payment_intent_id
            pi = {
                "id": pi_id,
                "amount": res.amount_cents,
                "currency": intent.currency,
                "status": "requires_payment_method",
            }
            webhook_delivery.enqueue(_event("payment_intent.created", pi, intent.intent_id))
            if res.status == PaymentStatus.SUCCEEDED:
                pi["status"] = "succeeded"
                webhook_delivery.enqueue(_event("payment_intent.succeeded", pi, intent.intent_id))
            elif res.status == PaymentStatus.PROCESSING and pi_id in client_async_failures():
                # The async payment fails shortly after; Stripe reports it by webhook only.
                failed = client.settle_async_failure(pi_id)
                if failed is not None:
                    async_settled.append(pi_id)
                    webhook_delivery.enqueue(
                        _event("payment_intent.payment_failed", failed, intent.intent_id)
                    )
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

        # 3. Step 3: Deliver webhooks (duplicates, reordering, replays, forgeries)
        webhook_delivery.flush()

        # 4. Step 4: Optional abort — the agent must release anything still held
        if self.scenario.abort:
            abort = getattr(self.agent, "abort", None)
            if callable(abort):
                abort(client)
            else:
                logger.warning("Scenario requests abort but agent has no abort(client) method")

        # 5. Step 5: Re-read PIs whose state changed asynchronously, so invariants
        #    compare the agent's ledger against Stripe's final state.
        for pi_id in async_settled:
            client.retrieve_payment_intent(pi_id)

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
