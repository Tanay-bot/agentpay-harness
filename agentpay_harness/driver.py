"""The scenario driver — executes agents through a purchase loop under adversarial conditions.

Applies fault schedules deterministically, delivers chaotic webhooks, records all
interactions, and evaluates payment invariants.

A run goes through these phases:

1. **Plan & pay** — each agent instance plans the task and pays its intents
   through a recording, fault-injecting Stripe client.  With ``agents > 1`` the
   instances run in parallel threads against one :class:`SharedBudget`, and an
   ``interleave`` fault fixes the order in which they pass budget checkpoints.
2. **Webhooks** — queued events are delivered (duplicated, reordered, replayed,
   forged, clock-skewed), each routed to the agent that made the payment.
   With ``webhook_source: stripe_cli`` they are genuine Stripe deliveries instead.
3. **Abort / settle / refund** — optional lifecycle steps the scenario asks for.
4. **Invariants** — checked against recorded requests, deliveries and the
   combined ledger of every agent.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.budget import InterleavingScheduler, SharedBudget
from agentpay_harness.faults.actions import WebhookActionType
from agentpay_harness.faults.engine import FaultEngine
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.safety import load_stripe_key_from_env, validate_stripe_key
from agentpay_harness.stripecli import StripeCLIError, StripeCLISession, wait_for
from agentpay_harness.stripeio.client import RecordingStripeClient
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    FaultType,
    Intent,
    LedgerEntry,
    PaymentResult,
    PaymentStatus,
    RunResult,
    ScenarioConfig,
    SessionContext,
    StripeEvent,
    Task,
    require_usd,
)
from agentpay_harness.webhooks.receiver import WebhookDeliverySystem, WebhookReceiver

logger = logging.getLogger(__name__)


class HarnessDriver:
    """Runs a ScenarioConfig against one or more AgentUnderTest instances.

    ``agent`` is the first instance.  When the scenario sets ``agents: N``, the
    other N-1 come from ``agent_factory`` (default: ``type(agent)()``).
    """

    def __init__(
        self,
        agent: AgentUnderTest,
        scenario: ScenarioConfig,
        stripe_key: str | None = None,
        stripe_base_url: str | None = None,
        force_offline: bool = True,
        agent_factory: Callable[[], AgentUnderTest] | None = None,
    ) -> None:
        self.agent = agent
        self.scenario = scenario
        self.stripe_key = stripe_key or load_stripe_key_from_env()
        validate_stripe_key(self.stripe_key)
        self.stripe_base_url = stripe_base_url
        self.force_offline = force_offline
        factory = agent_factory or type(agent)
        self.agents: list[AgentUnderTest] = [agent] + [
            factory() for _ in range(scenario.agents - 1)
        ]
        self.scheduler: InterleavingScheduler | None = None

    # ------------------------------------------------------------------

    def run(self) -> RunResult:
        """Execute the scenario and evaluate all asserted invariants."""
        t_start = time.perf_counter()
        sc = self.scenario

        session = SessionContext(
            seed=sc.seed,
            budget_cents=sc.budget_cents,
            stripe_key_prefix=self.stripe_key[:8],
            start_time=datetime.now(tz=timezone.utc),
        )

        recorder = EventRecorder()
        fault_engine = FaultEngine(sc.faults, seed=sc.seed)
        client = RecordingStripeClient(
            api_key=self.stripe_key,
            base_url=self.stripe_base_url,
            fault_interceptor=fault_engine.intercept_request,
            force_offline=self.force_offline,
        )

        # Events are routed to the agent that created the payment (by intent_id);
        # events nobody owns (forgeries, genuine CLI events) go to every agent.
        owners: dict[str, int] = {}
        owners_lock = threading.Lock()

        def route(event: StripeEvent) -> None:
            intent_id = ((event.data.get("object") or {}).get("metadata") or {}).get("intent_id")
            with owners_lock:
                idx = owners.get(str(intent_id)) if intent_id else None
            targets = self.agents if idx is None else [self.agents[idx]]
            for agent in targets:
                agent.on_event(event)

        webhook_delivery = WebhookDeliverySystem(
            target_callback=route,
            fault_engine=fault_engine,
            recorder=recorder,
            reorder_window=max(
                (f.window or 3 for f in sc.faults if f.type is FaultType.REORDER_WEBHOOKS),
                default=0,
            ),
            seed=sc.seed,
            clock_skews=[
                (f.event, int(f.skew_s or 0)) for f in sc.faults if f.type is FaultType.CLOCK_SKEW
            ],
        )

        shared: SharedBudget | None = None
        if len(self.agents) > 1:
            interleave = next((f for f in sc.faults if f.type is FaultType.INTERLEAVE), None)
            if interleave is not None:
                self.scheduler = (
                    InterleavingScheduler(interleave.schedule, len(self.agents))
                    if interleave.schedule
                    else InterleavingScheduler.seeded(sc.seed, len(self.agents))
                )
            shared = SharedBudget(sc.budget_cents, self.scheduler)

        if sc.webhook_source == "stripe_cli":
            self._run_stripe_cli(webhook_delivery, fault_engine, shared)
        else:
            async_settled = self._plan_and_pay(
                client, webhook_delivery, owners, owners_lock, shared
            )
            webhook_delivery.flush()
            # Re-read PIs that changed asynchronously so invariants compare the
            # ledger against Stripe's final state.
            for pi_id in async_settled:
                client.retrieve_payment_intent(pi_id)

        self._lifecycle(client)

        for req in client.get_records():
            recorder.record_request(req)

        ledger: list[LedgerEntry] = [e for agent in self.agents for e in agent.ledger()]
        assertions = sc.assertions or list(DEFAULT_REGISTRY._checkers.keys())
        violations = DEFAULT_REGISTRY.check_all(
            ids=assertions, recorder=recorder, ledger=ledger, session=session
        )

        return RunResult(
            session=session,
            events=client.get_records(),
            violations=violations,
            passed=len(violations) == 0,
            duration_seconds=time.perf_counter() - t_start,
        )

    # ------------------------------------------------------------------
    # Phase 1: plan & pay
    # ------------------------------------------------------------------

    def _task(self, shared: SharedBudget | None, **extra: Any) -> Task:
        metadata: dict[str, Any] = {"capture_method": self.scenario.capture_method, **extra}
        if shared is not None:
            metadata["budget_store"] = shared
        return Task(
            description=self.scenario.task,
            budget_cents=self.scenario.budget_cents,
            metadata=metadata,
        )

    def _plan_and_pay(
        self,
        client: RecordingStripeClient,
        webhook_delivery: WebhookDeliverySystem,
        owners: dict[str, int],
        owners_lock: threading.Lock,
        shared: SharedBudget | None,
    ) -> list[str]:
        sc = self.scenario
        clock_lock = threading.Lock()
        clock = {"next": int(time.time())}
        async_settled: list[str] = []
        pending_async: set[str] = set()

        def tick() -> int:
            with clock_lock:
                clock["next"] += 1
                return clock["next"]

        def make_event(evt_type: str, pi: dict[str, Any], intent_id: str) -> StripeEvent:
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
                created=tick(),
                livemode=False,
            )

        def async_failures() -> set[str]:
            with clock_lock:
                pending_async.update(client.take_async_failures())
                return set(pending_async)

        def execute(agent: AgentUnderTest, intent: Intent) -> PaymentResult:
            res = agent.pay(intent, client)
            pi_id = res.stripe_payment_intent_id
            if not pi_id:
                return res
            pi = {
                "id": pi_id,
                "amount": res.amount_cents,
                "currency": intent.currency,
                "status": "requires_payment_method",
            }
            webhook_delivery.enqueue(make_event("payment_intent.created", pi, intent.intent_id))
            if res.status == PaymentStatus.SUCCEEDED:
                pi["status"] = "succeeded"
                webhook_delivery.enqueue(
                    make_event("payment_intent.succeeded", pi, intent.intent_id)
                )
            elif res.status == PaymentStatus.PROCESSING and pi_id in async_failures():
                # The async payment fails shortly after; Stripe reports it by webhook only.
                failed = client.settle_async_failure(pi_id)
                if failed is not None:
                    with clock_lock:
                        async_settled.append(pi_id)
                    webhook_delivery.enqueue(
                        make_event("payment_intent.payment_failed", failed, intent.intent_id)
                    )
            return res

        def plan(index: int, agent: AgentUnderTest) -> list[Intent]:
            intents = agent.plan(self._task(shared))
            if sc.capture_method != "automatic":
                intents = [replace(i, capture_method=sc.capture_method) for i in intents]
            for intent in intents:
                # USD-only: fail loudly instead of summing euros into a dollar budget.
                require_usd(intent.currency, f"agent {index} intent {intent.intent_id}")
            with owners_lock:
                owners.update({i.intent_id: index for i in intents})
            return intents

        def run_agent(index: int, agent: AgentUnderTest) -> None:
            if self.scheduler is not None:
                self.scheduler.bind(index)
            try:
                intents = plan(index, agent)
                if sc.concurrency > 1 and len(intents) > 1 and len(self.agents) == 1:
                    with concurrent.futures.ThreadPoolExecutor(sc.concurrency) as pool:
                        for fut in [pool.submit(execute, agent, i) for i in intents]:
                            try:
                                fut.result()
                            except Exception as exc:
                                logger.exception("Error executing intent: %s", exc)
                else:
                    for intent in intents:
                        execute(agent, intent)
            finally:
                if self.scheduler is not None:
                    self.scheduler.finish(index)

        if len(self.agents) == 1:
            run_agent(0, self.agents[0])
        else:
            errors: list[BaseException] = []

            def guarded(index: int, agent: AgentUnderTest) -> None:
                try:
                    run_agent(index, agent)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=guarded, args=(i, a), name=f"agent-{i}")
                for i, a in enumerate(self.agents)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if errors:
                raise errors[0]

        return async_settled

    # ------------------------------------------------------------------
    # Phase 2 (alternative): genuine Stripe deliveries via the Stripe CLI
    # ------------------------------------------------------------------

    def _run_stripe_cli(
        self,
        webhook_delivery: WebhookDeliverySystem,
        fault_engine: FaultEngine,
        shared: SharedBudget | None,
    ) -> None:
        sc = self.scenario
        collected: list[tuple[bytes, str]] = []
        collected_lock = threading.Lock()

        def collect(body: bytes, header: str) -> None:
            with collected_lock:
                collected.append((body, header))

        def seen_types() -> set[str]:
            with collected_lock:
                return {json.loads(body).get("type", "") for body, _ in collected}

        receiver = WebhookReceiver(event_handler=lambda e: None, raw_handler=collect)
        receiver.start()
        try:
            cli = StripeCLISession(self.stripe_key, receiver.url)
            secret = cli.webhook_secret()
            webhook_delivery.signing_secret = secret
            # Agents only observe in this mode; plan() hands them the signing secret.
            for agent in self.agents:
                agent.plan(self._task(shared, webhook_secret=secret))
            with cli:
                for trigger in sc.cli_triggers:
                    cli.trigger(trigger)
                expected = set(sc.cli_triggers)
                if not wait_for(lambda: expected <= seen_types(), sc.cli_timeout_s):
                    raise StripeCLIError(
                        f"Timed out after {sc.cli_timeout_s}s waiting for events "
                        f"{sorted(expected - seen_types())}; received {sorted(seen_types())}"
                    )
        finally:
            receiver.stop()

        with collected_lock:
            deliveries = list(collected)
        for body, header in deliveries:
            data = json.loads(body)
            event = StripeEvent(
                event_id=str(data["id"]),
                type=str(data.get("type", "unknown")),
                data=data.get("data", {}),
                created=int(data.get("created", 0)),
                livemode=bool(data.get("livemode", False)),
            )
            for action in fault_engine.intercept_webhook(event):
                if action.action_type is WebhookActionType.FORGE:
                    webhook_delivery.send_signed(
                        action.event, WebhookDeliverySystem.FORGED_SECRET, action.fault_description
                    )
                elif action.action_type is not WebhookActionType.DROP:
                    webhook_delivery.deliver_genuine(event, body, header)

    # ------------------------------------------------------------------
    # Phase 3: abort / settle / refund
    # ------------------------------------------------------------------

    def _lifecycle(self, client: RecordingStripeClient) -> None:
        sc = self.scenario
        steps = [
            (sc.abort, "abort"),
            (sc.settle, "capture_authorized"),
            (sc.refund, "refund_captured"),
        ]
        for enabled, method_name in steps:
            if not enabled:
                continue
            for agent in self.agents:
                method = getattr(agent, method_name, None)
                if callable(method):
                    method(client)
                else:
                    logger.warning(
                        "Scenario needs %s(client) but %s does not implement it",
                        method_name,
                        type(agent).__name__,
                    )
