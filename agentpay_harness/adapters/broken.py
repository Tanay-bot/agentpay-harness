"""Deliberately broken agent variants for 'tests for the tester'.

Each class extends :class:`ReferenceAgent` but surgically breaks exactly one
invariant so the harness can verify that its checks detect the violation.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from agentpay_harness.adapters.protocol import StripeClient
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.types import (
    Intent,
    LedgerEntry,
    LedgerEntryStatus,
    PaymentResult,
    PaymentStatus,
    StripeEvent,
    Task,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# I1 + I4 — Broken idempotency
# ---------------------------------------------------------------------------


class BrokenIdempotencyAgent(ReferenceAgent):
    """Generates a fresh UUID idempotency key on every retry.

    Breaks:
        I1 — Idempotency keys must be present on every create call.
        I4 — The same idempotency key must be used across retries.

    A fresh key on every attempt means retries create *new* PaymentIntents
    instead of being deduplicated, risking duplicate charges.
    """

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Override pay to use a DIFFERENT idempotency key every time."""
        # Budget gate — still correct, only idempotency is broken.
        with self._lock:
            remaining = self._budget_cents - self._spent_cents
            if intent.amount_cents > remaining:
                return PaymentResult(
                    intent_id=intent.intent_id,
                    status=PaymentStatus.FAILED,
                    amount_cents=intent.amount_cents,
                    error=f"Budget exceeded: need {intent.amount_cents}, have {remaining}",
                )
            self._spent_cents += intent.amount_cents
            pending_entry = LedgerEntry(
                intent_id=intent.intent_id,
                amount_cents=intent.amount_cents,
                currency=intent.currency,
                status=LedgerEntryStatus.PENDING,
                description=intent.description,
                metadata=dict(intent.metadata),
            )
            self._ledger.append(pending_entry)

        # BUG: Generate a NEW idempotency key on every attempt (breaks I1, I4).
        last_exc: Exception | None = None
        for attempt in range(5):
            bad_key = uuid4().hex  # <- THIS IS THE BUG
            try:
                result = client.create_payment_intent(
                    intent.amount_cents,
                    intent.currency,
                    idempotency_key=bad_key,
                    metadata=dict(intent.metadata or {}, intent_id=intent.intent_id),
                )
                if isinstance(result, dict):
                    status_code = result.get("_status_code")
                    if status_code == 429:
                        delay = self._backoff_delay(attempt)
                        time.sleep(delay)
                        continue
                    if isinstance(status_code, int) and status_code >= 500:
                        delay = self._backoff_delay(attempt)
                        time.sleep(delay)
                        continue
                    if status_code == 0 or (
                        result.get("error")
                        and any(
                            word in str(result.get("error")).lower()
                            for word in ("timeout", "dropped", "connection")
                        )
                    ):
                        time.sleep(self._backoff_delay(attempt))
                        continue

                stripe_pi_id = result.get("id", "")
                status = self._map_stripe_status(result.get("status", ""))
                with self._lock:
                    self._payment_intent_ids.append(stripe_pi_id)
                    new_status = (
                        LedgerEntryStatus.CAPTURED
                        if status in (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING)
                        else LedgerEntryStatus.FAILED
                    )
                    if new_status == LedgerEntryStatus.FAILED:
                        self._spent_cents -= intent.amount_cents
                    self._update_ledger_status(intent.intent_id, new_status, stripe_pi_id)

                return PaymentResult(
                    intent_id=intent.intent_id,
                    stripe_payment_intent_id=stripe_pi_id,
                    status=status,
                    amount_cents=intent.amount_cents,
                    idempotency_key=bad_key,
                )
            except Exception as exc:
                last_exc = exc
                time.sleep(self._backoff_delay(attempt))

        with self._lock:
            self._spent_cents -= intent.amount_cents
            self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED)
        return PaymentResult(
            intent_id=intent.intent_id,
            status=PaymentStatus.FAILED,
            amount_cents=intent.amount_cents,
            error=str(last_exc) if last_exc else "Exhausted retries",
        )


# ---------------------------------------------------------------------------
# I2 — Broken budget gate (no lock)
# ---------------------------------------------------------------------------


class BrokenBudgetAgent(ReferenceAgent):
    """Checks budget WITHOUT holding the lock.

    Breaks:
        I2 — Budget gate must be checked under lock before every charge.

    The TOCTOU gap allows concurrent calls to each see sufficient budget and
    all proceed, exceeding the total budget.
    """

    def plan(self, task: Task) -> list[Intent]:
        """Split task into 3 concurrent intents to trigger TOCTOU race."""
        self._budget_cents = task.budget_cents
        self._spent_cents = 0
        chunk = 7500  # 3 * $75 = $225 > $200 budget
        return [
            Intent(
                task_id=task.task_id,
                amount_cents=chunk,
                description=f"Pack {i + 1}",
                idempotency_key=f"idem_pack_{task.task_id}_{i}",
                metadata={"intent_id": f"intent_pack_{task.task_id}_{i}"},
            )
            for i in range(3)
        ]

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Override pay to check budget WITHOUT the lock."""
        idempotency_key = intent.idempotency_key or self._stable_idempotency_key(
            intent.intent_id, 0
        )

        # BUG: Read budget state WITHOUT holding the lock (breaks I2).
        remaining = self._budget_cents - self._spent_cents
        if intent.amount_cents > remaining:
            return PaymentResult(
                intent_id=intent.intent_id,
                status=PaymentStatus.FAILED,
                amount_cents=intent.amount_cents,
                error=f"Budget exceeded: need {intent.amount_cents}, have {remaining}",
                idempotency_key=idempotency_key,
            )

        # TOCTOU window: allow concurrent threads to check before any writes
        time.sleep(0.02)

        # BUG: Also update spent WITHOUT the lock.
        self._spent_cents += intent.amount_cents

        with self._lock:
            pending_entry = LedgerEntry(
                intent_id=intent.intent_id,
                amount_cents=intent.amount_cents,
                currency=intent.currency,
                status=LedgerEntryStatus.PENDING,
                description=intent.description,
                metadata=dict(intent.metadata),
            )
            self._ledger.append(pending_entry)

        try:
            pi = self._create_with_retry(
                client,
                amount=intent.amount_cents,
                currency=intent.currency,
                idempotency_key=idempotency_key,
                metadata=intent.metadata,
            )
        except Exception as exc:
            self._spent_cents -= intent.amount_cents
            with self._lock:
                self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED)
            return PaymentResult(
                intent_id=intent.intent_id,
                status=PaymentStatus.FAILED,
                amount_cents=intent.amount_cents,
                error=str(exc),
                idempotency_key=idempotency_key,
            )

        stripe_pi_id = pi.get("id", "")
        status = self._map_stripe_status(pi.get("status", ""))
        with self._lock:
            self._payment_intent_ids.append(stripe_pi_id)
            if status not in (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING):
                self._spent_cents -= intent.amount_cents
                self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED, stripe_pi_id)
            else:
                # BUG: Charge succeeded on Stripe, but agent never writes to ledger
                pass
        return PaymentResult(
            intent_id=intent.intent_id,
            stripe_payment_intent_id=stripe_pi_id,
            status=status,
            amount_cents=intent.amount_cents,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# I3 + I11 — Broken ledger recording
# ---------------------------------------------------------------------------


class BrokenLedgerAgent(ReferenceAgent):
    """Sometimes fails to record a charge in the ledger.

    Breaks:
        I3  — Every charge attempt must be recorded in the ledger.
        I11 — Ledger entries must always match Stripe state.

    Randomly skips ledger recording ~50% of the time, causing the agent's
    internal view to diverge from Stripe's actual state.
    """

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Override pay to sometimes skip ledger recording."""
        idempotency_key = intent.idempotency_key or self._stable_idempotency_key(
            intent.intent_id, 0
        )

        with self._lock:
            remaining = self._budget_cents - self._spent_cents
            if intent.amount_cents > remaining:
                return PaymentResult(
                    intent_id=intent.intent_id,
                    status=PaymentStatus.FAILED,
                    amount_cents=intent.amount_cents,
                    error=f"Budget exceeded: need {intent.amount_cents}, have {remaining}",
                    idempotency_key=idempotency_key,
                )
            self._spent_cents += intent.amount_cents

            # BUG (Partial-failure amnesia): Charge succeeds on Stripe but agent
            # completely forgets to record it in its own ledger (breaks I3, I11).
            pass

        try:
            pi = self._create_with_retry(
                client,
                amount=intent.amount_cents,
                currency=intent.currency,
                idempotency_key=idempotency_key,
                metadata=intent.metadata,
            )
        except Exception as exc:
            with self._lock:
                self._spent_cents -= intent.amount_cents
                self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED)
            return PaymentResult(
                intent_id=intent.intent_id,
                status=PaymentStatus.FAILED,
                amount_cents=intent.amount_cents,
                error=str(exc),
                idempotency_key=idempotency_key,
            )

        stripe_pi_id = pi.get("id", "")
        status = self._map_stripe_status(pi.get("status", ""))
        with self._lock:
            self._payment_intent_ids.append(stripe_pi_id)
            if status not in (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING):
                self._spent_cents -= intent.amount_cents
                self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED, stripe_pi_id)
            else:
                # BUG: Charge succeeded on Stripe, but agent never writes to ledger
                pass
        return PaymentResult(
            intent_id=intent.intent_id,
            stripe_payment_intent_id=stripe_pi_id,
            status=status,
            amount_cents=intent.amount_cents,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# I5 — Broken retry parameters
# ---------------------------------------------------------------------------


class BrokenRetryParamsAgent(ReferenceAgent):
    """Changes the amount on retry with the same idempotency key.

    Breaks:
        I5 — Retry requests must use identical parameters.

    Stripe will reject the retry with an idempotency-key-mismatch error,
    or worse, the agent may silently charge the wrong amount.
    """

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Override pay to mutate amount on retry."""
        idempotency_key = intent.idempotency_key or self._stable_idempotency_key(
            intent.intent_id, 0
        )

        with self._lock:
            remaining = self._budget_cents - self._spent_cents
            if intent.amount_cents > remaining:
                return PaymentResult(
                    intent_id=intent.intent_id,
                    status=PaymentStatus.FAILED,
                    amount_cents=intent.amount_cents,
                    error=f"Budget exceeded: need {intent.amount_cents}, have {remaining}",
                    idempotency_key=idempotency_key,
                )
            self._spent_cents += intent.amount_cents
            pending_entry = LedgerEntry(
                intent_id=intent.intent_id,
                amount_cents=intent.amount_cents,
                currency=intent.currency,
                status=LedgerEntryStatus.PENDING,
                description=intent.description,
                metadata=dict(intent.metadata),
            )
            self._ledger.append(pending_entry)

        last_exc: Exception | None = None
        for attempt in range(5):
            # BUG: Add a penny on each retry with the SAME idempotency key (breaks I5).
            mutated_amount = intent.amount_cents + attempt
            try:
                result = client.create_payment_intent(
                    mutated_amount,  # <- THIS IS THE BUG
                    intent.currency,
                    idempotency_key=idempotency_key,
                    metadata=dict(intent.metadata),
                )
                if isinstance(result, dict):
                    status_code = result.get("_status_code")
                    if status_code == 429:
                        time.sleep(self._backoff_delay(attempt))
                        continue
                    if isinstance(status_code, int) and status_code >= 500:
                        time.sleep(self._backoff_delay(attempt))
                        continue

                stripe_pi_id = result.get("id", "")
                status = self._map_stripe_status(result.get("status", ""))
                with self._lock:
                    self._payment_intent_ids.append(stripe_pi_id)
                    if status not in (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING):
                        self._spent_cents -= intent.amount_cents
                        self._update_ledger_status(
                            intent.intent_id, LedgerEntryStatus.FAILED, stripe_pi_id
                        )
                    else:
                        self._update_ledger_status(
                            intent.intent_id, LedgerEntryStatus.CAPTURED, stripe_pi_id
                        )
                return PaymentResult(
                    intent_id=intent.intent_id,
                    stripe_payment_intent_id=stripe_pi_id,
                    status=status,
                    amount_cents=mutated_amount,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                last_exc = exc
                time.sleep(self._backoff_delay(attempt))

        with self._lock:
            self._spent_cents -= intent.amount_cents
            self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED)
        return PaymentResult(
            intent_id=intent.intent_id,
            status=PaymentStatus.FAILED,
            amount_cents=intent.amount_cents,
            error=str(last_exc) if last_exc else "Exhausted retries",
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# I6 — Broken webhook deduplication
# ---------------------------------------------------------------------------


class BrokenWebhookDedupeAgent(ReferenceAgent):
    """Processes every webhook without deduplication.

    Breaks:
        I6 — Duplicate webhook events must not cause duplicate side-effects.

    Without deduplication, a duplicated ``payment_intent.succeeded`` event
    will credit the ledger twice, corrupting the agent's financial state.
    """

    def on_event(self, event: StripeEvent) -> None:
        """Process every event without checking for duplicates."""
        if event.livemode:
            logger.warning("Rejecting live-mode event %s", event.event_id)
            return

        # BUG: No deduplication check — process EVERY event (breaks I6).
        # (Skipping: self._seen_event_ids check)

        event_type = event.type
        pi_data: dict[str, Any] = event.data.get("object", {})
        stripe_pi_id: str = pi_data.get("id", "")

        if event_type == "payment_intent.succeeded":
            with self._lock:
                # BUG: Missing deduplication — appends duplicate fulfillment entry
                # on every delivery of the same event_id (breaks I6).
                entry = LedgerEntry(
                    intent_id=pi_data.get("metadata", {}).get("intent_id", stripe_pi_id),
                    stripe_payment_intent_id=stripe_pi_id,
                    amount_cents=pi_data.get("amount", 5000),
                    currency=pi_data.get("currency", "usd"),
                    status=LedgerEntryStatus.CAPTURED,
                    description="Fulfillment credit from webhook",
                    metadata={"event_id": event.event_id},
                )
                self._ledger.append(entry)
        elif event_type == "payment_intent.payment_failed":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.FAILED)
                    self._spent_cents -= entry.amount_cents
        elif event_type == "payment_intent.canceled":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.VOIDED)
                    self._spent_cents -= entry.amount_cents
        elif event_type == "charge.refunded":
            with self._lock:
                charge_pi_id = pi_data.get("payment_intent", "")
                if charge_pi_id:
                    entry = self._find_ledger_by_stripe_id(charge_pi_id)
                    if entry is not None:
                        self._update_ledger_by_stripe_id(charge_pi_id, LedgerEntryStatus.REFUNDED)
                        self._spent_cents -= entry.amount_cents


# ---------------------------------------------------------------------------
# I7 — Broken webhook ordering assumption
# ---------------------------------------------------------------------------


class BrokenWebhookOrderAgent(ReferenceAgent):
    """Assumes webhooks arrive in strict chronological order.

    Breaks:
        I7 — Agent must not assume webhook delivery order.

    Rejects any event whose timestamp is older than the last seen timestamp,
    which causes it to miss legitimate out-of-order events.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_event_timestamp: int = 0

    def on_event(self, event: StripeEvent) -> None:
        """Reject events that arrive out of chronological order."""
        if event.livemode:
            logger.warning("Rejecting live-mode event %s", event.event_id)
            return

        with self._lock:
            if event.event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event.event_id)

        # BUG: Reject out-of-order events (breaks I7).
        if event.created < self._last_event_timestamp:
            logger.debug(
                "Dropping out-of-order event %s (ts=%d < last=%d)",
                event.event_id,
                event.created,
                self._last_event_timestamp,
            )
            return  # <- THIS IS THE BUG: legitimate events are silently dropped

        self._last_event_timestamp = event.created

        # Proceed with normal processing for in-order events.
        event_type = event.type
        pi_data: dict[str, Any] = event.data.get("object", {})
        stripe_pi_id: str = pi_data.get("id", "")

        if event_type == "payment_intent.succeeded":
            with self._lock:
                # BUG: Missing deduplication — appends duplicate fulfillment entry
                # on every delivery of the same event_id (breaks I6).
                entry = LedgerEntry(
                    intent_id=pi_data.get("metadata", {}).get("intent_id", stripe_pi_id),
                    stripe_payment_intent_id=stripe_pi_id,
                    amount_cents=pi_data.get("amount", 5000),
                    currency=pi_data.get("currency", "usd"),
                    status=LedgerEntryStatus.CAPTURED,
                    description="Fulfillment credit from webhook",
                    metadata={"event_id": event.event_id},
                )
                self._ledger.append(entry)
        elif event_type == "payment_intent.payment_failed":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.FAILED)
                    self._spent_cents -= entry.amount_cents
        elif event_type == "payment_intent.canceled":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.VOIDED)
                    self._spent_cents -= entry.amount_cents


# ---------------------------------------------------------------------------
# I8 — Broken webhook verification
# ---------------------------------------------------------------------------


class BrokenWebhookVerifyAgent(ReferenceAgent):
    """Skips webhook signature verification.

    Breaks:
        I8 — Webhook signatures must be verified to prevent spoofing.

    Accepts all events without any verification, making the agent vulnerable
    to forged webhook payloads.
    """

    def on_event(self, event: StripeEvent) -> None:
        """Accept events without checking livemode or signatures."""
        # BUG: No livemode check, no signature verification (breaks I8).
        # Proceeds directly to processing.

        with self._lock:
            if event.event_id in self._seen_event_ids:
                return
            self._seen_event_ids.add(event.event_id)

        event_type = event.type
        pi_data: dict[str, Any] = event.data.get("object", {})
        stripe_pi_id: str = pi_data.get("id", "")

        if event_type == "payment_intent.succeeded":
            with self._lock:
                # BUG: Missing deduplication — appends duplicate fulfillment entry
                # on every delivery of the same event_id (breaks I6).
                entry = LedgerEntry(
                    intent_id=pi_data.get("metadata", {}).get("intent_id", stripe_pi_id),
                    stripe_payment_intent_id=stripe_pi_id,
                    amount_cents=pi_data.get("amount", 5000),
                    currency=pi_data.get("currency", "usd"),
                    status=LedgerEntryStatus.CAPTURED,
                    description="Fulfillment credit from webhook",
                    metadata={"event_id": event.event_id},
                )
                self._ledger.append(entry)
        elif event_type == "payment_intent.payment_failed":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.FAILED)
                    self._spent_cents -= entry.amount_cents
        elif event_type == "payment_intent.canceled":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.VOIDED)
                    self._spent_cents -= entry.amount_cents
        elif event_type == "charge.refunded":
            with self._lock:
                charge_pi_id = pi_data.get("payment_intent", "")
                if charge_pi_id:
                    entry = self._find_ledger_by_stripe_id(charge_pi_id)
                    if entry is not None:
                        self._update_ledger_by_stripe_id(charge_pi_id, LedgerEntryStatus.REFUNDED)
                        self._spent_cents -= entry.amount_cents


# ---------------------------------------------------------------------------
# I9 — Broken abort cleanup
# ---------------------------------------------------------------------------


class BrokenAbortAgent(ReferenceAgent):
    """Abandons payment intents without canceling them on abort.

    Breaks:
        I9 — On abort, all uncaptured payment intents must be canceled.

    Leaves uncaptured PaymentIntents dangling, which could be captured later
    by Stripe's automatic capture, resulting in unexpected charges.
    """

    def abort(self, client: StripeClient) -> None:
        """Do nothing on abort — leave PaymentIntents dangling."""
        # BUG: No cleanup at all (breaks I9).
        logger.debug("BrokenAbortAgent.abort() called — doing nothing")


# ---------------------------------------------------------------------------
# I10 — Broken backoff
# ---------------------------------------------------------------------------


class BrokenBackoffAgent(ReferenceAgent):
    """Retries immediately on 429 without backoff.

    Breaks:
        I10 — Must use exponential backoff with jitter on rate-limit responses.

    Hammering the API without backoff will cause cascading rate limits and
    potentially get the API key temporarily blocked.
    """

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Return zero delay — no backoff at all."""
        # BUG: No backoff, no jitter (breaks I10).
        return 0.0
