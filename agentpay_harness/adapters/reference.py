"""Reference agent implementation that correctly satisfies all harness invariants.

Invariants upheld:
    I1  — Idempotency keys on every create call, derived stably from intent_id.
    I2  — Budget gate checked under lock before every charge.
    I3  — Ledger always records every charge attempt (atomic with budget check).
    I4  — Stable idempotency key across retries (same key for same intent).
    I5  — Retry uses identical parameters (never mutates amount/currency).
    I6  — Webhook deduplication by event_id.
    I7  — No assumption about webhook delivery order.
    I8  — Webhook signature verification (placeholder — delegates to caller).
    I9  — On abort, cancel all uncaptured payment intents.
    I10 — Exponential backoff with jitter on 429 (rate-limit) responses.
    I11 — Ledger entries always match Stripe state.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import threading
import time
from decimal import Decimal
from typing import Any

from agentpay_harness.adapters.protocol import StripeClient
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

# Retry configuration
_MAX_RETRIES = 5
_BASE_DELAY_S = 0.5
_MAX_DELAY_S = 30.0


_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_ITEM_PURCHASE_RE = re.compile(
    r"\b(?P<count>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"[^$]*?\b(?:at|under|for)\s+\$(?P<price>\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def _parse_item_purchase(description: str) -> tuple[int, int] | None:
    """Parse "<count> ... at $<price> [each]" into (count, unit price in cents)."""
    match = _ITEM_PURCHASE_RE.search(description)
    if match is None:
        return None
    raw_count = match.group("count").lower()
    count = int(raw_count) if raw_count.isdigit() else _NUMBER_WORDS[raw_count]
    unit_cents = int(Decimal(match.group("price")) * 100)
    if count < 1 or unit_cents < 1:
        return None
    return count, unit_cents


class _RateLimitError(Exception):
    """Raised when Stripe returns HTTP 429."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Rate limited by Stripe")
        self.retry_after = retry_after


class _StripeAPIError(Exception):
    """Raised when Stripe returns a 5xx server error."""

    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"Stripe server error: {status}")
        self.status = status
        self.body = body


class ReferenceAgent:
    """A reference agent that correctly handles all payment invariants.

    This implementation is intentionally simple and correct — it is the
    baseline that every harness scenario should pass against.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ledger: list[LedgerEntry] = []
        self._seen_event_ids: set[str] = set()
        self._payment_intent_ids: list[str] = []  # track for abort cleanup
        self._spent_cents: int = 0
        self._budget_cents: int = 0

    # ------------------------------------------------------------------
    # AgentUnderTest protocol
    # ------------------------------------------------------------------

    def plan(self, task: Task) -> list[Intent]:
        """Parse the task and produce a list of intents.

        Tasks shaped like "buy three packs at $75 each" become one intent per
        item, so concurrency and budget scenarios actually exercise the budget
        gate.  Anything else falls back to a single intent for the full budget.
        """
        with self._lock:
            self._budget_cents = task.budget_cents
            self._spent_cents = 0

        parsed = _parse_item_purchase(task.description)
        if parsed is not None:
            count, unit_cents = parsed
            return [
                Intent(
                    task_id=task.task_id,
                    amount_cents=unit_cents,
                    description=f"{task.description} (item {i + 1} of {count})",
                    idempotency_key=self._stable_idempotency_key(task.task_id, i),
                )
                for i in range(count)
            ]

        return [
            Intent(
                task_id=task.task_id,
                amount_cents=task.budget_cents,
                description=task.description,
                idempotency_key=self._stable_idempotency_key(task.task_id, 0),
            ),
        ]

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Execute a payment intent with proper budget checking and retries.

        Uses a stable idempotency key derived from the intent_id so that
        retries are safe.
        """
        # Derive a stable idempotency key from the intent_id (I1, I4).
        idempotency_key = intent.idempotency_key or self._stable_idempotency_key(
            intent.intent_id, 0
        )

        # Budget gate — check under lock (I2, I3, I11).
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

            # Optimistically reserve the budget (I2).
            self._spent_cents += intent.amount_cents

            # Record a PENDING ledger entry (I3, I11).
            pending_entry = LedgerEntry(
                intent_id=intent.intent_id,
                amount_cents=intent.amount_cents,
                currency=intent.currency,
                status=LedgerEntryStatus.PENDING,
                description=intent.description,
                metadata=dict(intent.metadata),
            )
            self._ledger.append(pending_entry)

        # Execute the Stripe call with retries + backoff (I10).
        try:
            pi = self._create_with_retry(
                client,
                amount=intent.amount_cents,
                currency=intent.currency,
                idempotency_key=idempotency_key,
                metadata=dict(intent.metadata or {}, intent_id=intent.intent_id),
            )
        except Exception as exc:
            # Roll back budget reservation on failure.
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

        stripe_pi_id: str = pi.get("id", "")
        status_str: str = pi.get("status", "")

        # Track for abort cleanup (I9).
        with self._lock:
            self._payment_intent_ids.append(stripe_pi_id)

        payment_status = self._map_stripe_status(status_str)

        # If payment didn't succeed, release budget and update ledger.
        if payment_status not in (PaymentStatus.SUCCEEDED, PaymentStatus.PROCESSING):
            with self._lock:
                self._spent_cents -= intent.amount_cents
                self._update_ledger_status(intent.intent_id, LedgerEntryStatus.FAILED, stripe_pi_id)
        else:
            with self._lock:
                self._update_ledger_status(
                    intent.intent_id, LedgerEntryStatus.CAPTURED, stripe_pi_id
                )

        return PaymentResult(
            intent_id=intent.intent_id,
            stripe_payment_intent_id=stripe_pi_id,
            status=payment_status,
            amount_cents=intent.amount_cents,
            idempotency_key=idempotency_key,
            request_id=pi.get("request_id"),
        )

    def on_event(self, event: StripeEvent) -> None:
        """Handle a Stripe webhook event with deduplication.

        - Deduplicates by event_id (I6).
        - Does not assume ordering (I7).
        - Checks livemode is False (I8 — basic verification).
        """
        # I8 — reject live-mode events.
        if event.livemode:
            logger.warning("Rejecting live-mode event %s", event.event_id)
            return

        # I6 — deduplicate by event_id.
        with self._lock:
            if event.event_id in self._seen_event_ids:
                logger.debug("Duplicate event %s, skipping", event.event_id)
                return
            self._seen_event_ids.add(event.event_id)

        # I7 — handle events based on type, not assumed order.
        event_type = event.type
        pi_data: dict[str, Any] = event.data.get("object", {})
        stripe_pi_id: str = pi_data.get("id", "")
        if event_type == "payment_intent.succeeded":
            with self._lock:
                self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.CAPTURED)
        elif event_type == "payment_intent.payment_failed":
            with self._lock:
                entry = self._find_ledger_by_stripe_id(stripe_pi_id)
                if entry is not None:
                    self._update_ledger_by_stripe_id(stripe_pi_id, LedgerEntryStatus.FAILED)
                    # Release budget for failed payment.
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
        else:
            logger.debug("Ignoring unhandled event type: %s", event_type)

    def ledger(self) -> list[LedgerEntry]:
        """Return a snapshot of the current ledger."""
        with self._lock:
            return list(self._ledger)

    # ------------------------------------------------------------------
    # Abort / cleanup (I9)
    # ------------------------------------------------------------------

    def abort(self, client: StripeClient) -> None:
        """Cancel all uncaptured payment intents on abort (I9)."""
        with self._lock:
            pi_ids = list(self._payment_intent_ids)

        for pi_id in pi_ids:
            try:
                result = client.retrieve_payment_intent(pi_id)
                status = result.get("status", "")
                if status in (
                    "requires_payment_method",
                    "requires_capture",
                    "requires_confirmation",
                ):
                    client.cancel_payment_intent(pi_id)
                    with self._lock:
                        self._update_ledger_by_stripe_id(pi_id, LedgerEntryStatus.VOIDED)
                        entry = self._find_ledger_by_stripe_id(pi_id)
                        if entry is not None:
                            self._spent_cents -= entry.amount_cents
                    logger.info("Canceled uncaptured PaymentIntent %s", pi_id)
            except Exception:
                logger.exception("Failed to cancel PaymentIntent %s during abort", pi_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stable_idempotency_key(base: str, index: int) -> str:
        """Derive a deterministic idempotency key from a base string + index.

        Uses SHA-256 so the key is stable across retries (I1, I4).
        """
        raw = f"{base}:{index}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _create_with_retry(
        self,
        client: StripeClient,
        *,
        amount: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Call ``create_payment_intent`` with exponential backoff on 429/5xx.

        Parameters are NEVER mutated between retries (I5).
        The same idempotency key is used on every attempt (I4).
        """
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                result = client.create_payment_intent(
                    amount,
                    currency,
                    idempotency_key=idempotency_key,
                    metadata=dict(metadata),  # defensive copy
                )

                # Check for error responses encoded in the result dict.
                if isinstance(result, dict):
                    status_code = result.get("_status_code")
                    if status_code == 429:
                        retry_after = result.get("_retry_after")
                        raise _RateLimitError(
                            retry_after=float(retry_after) if retry_after else None
                        )
                    if isinstance(status_code, int) and status_code >= 500:
                        raise _StripeAPIError(status_code, result)
                    if status_code == 0 or (
                        result.get("error")
                        and any(
                            word in str(result.get("error")).lower()
                            for word in ("timeout", "dropped", "connection")
                        )
                    ):
                        last_exc = RuntimeError("Simulated network timeout/drop")
                        delay = self._backoff_delay(attempt)
                        time.sleep(delay)
                        continue

                return result

            except _RateLimitError as exc:
                last_exc = exc
                delay = exc.retry_after or self._backoff_delay(attempt)
                logger.warning(
                    "Rate limited (attempt %d/%d), backing off %.2fs",
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

            except _StripeAPIError as exc:
                last_exc = exc
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "Server error %d (attempt %d/%d), backing off %.2fs",
                    exc.status,
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                )
                time.sleep(delay)

        raise last_exc or RuntimeError("Exhausted retries with no recorded error")

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Calculate exponential backoff with full jitter (I10).

        Formula: min(MAX_DELAY, BASE * 2^attempt) * random(0, 1)
        """
        cap = min(_MAX_DELAY_S, _BASE_DELAY_S * (2**attempt))
        return cap * random.random()  # noqa: S311

    @staticmethod
    def _map_stripe_status(status: str) -> PaymentStatus:
        """Map a Stripe PaymentIntent status string to our enum."""
        mapping: dict[str, PaymentStatus] = {
            "succeeded": PaymentStatus.SUCCEEDED,
            "canceled": PaymentStatus.CANCELED,
            "processing": PaymentStatus.PROCESSING,
            "requires_action": PaymentStatus.REQUIRES_ACTION,
            "requires_payment_method": PaymentStatus.FAILED,
            "requires_confirmation": PaymentStatus.REQUIRES_ACTION,
            "requires_capture": PaymentStatus.PROCESSING,
        }
        return mapping.get(status, PaymentStatus.FAILED)

    def _update_ledger_status(
        self,
        intent_id: str,
        new_status: LedgerEntryStatus,
        stripe_pi_id: str | None = None,
    ) -> None:
        """Update the status of a ledger entry by intent_id.

        Must be called with ``self._lock`` held.
        """
        for idx, entry in enumerate(self._ledger):
            if entry.intent_id == intent_id:
                self._ledger[idx] = LedgerEntry(
                    entry_id=entry.entry_id,
                    intent_id=entry.intent_id,
                    stripe_payment_intent_id=stripe_pi_id or entry.stripe_payment_intent_id,
                    amount_cents=entry.amount_cents,
                    currency=entry.currency,
                    status=new_status,
                    description=entry.description,
                    created_at=entry.created_at,
                    metadata=entry.metadata,
                )
                return

    def _update_ledger_by_stripe_id(
        self,
        stripe_pi_id: str,
        new_status: LedgerEntryStatus,
    ) -> None:
        """Update the status of a ledger entry by Stripe PaymentIntent ID.

        Must be called with ``self._lock`` held.
        """
        for idx, entry in enumerate(self._ledger):
            if entry.stripe_payment_intent_id == stripe_pi_id:
                self._ledger[idx] = LedgerEntry(
                    entry_id=entry.entry_id,
                    intent_id=entry.intent_id,
                    stripe_payment_intent_id=entry.stripe_payment_intent_id,
                    amount_cents=entry.amount_cents,
                    currency=entry.currency,
                    status=new_status,
                    description=entry.description,
                    created_at=entry.created_at,
                    metadata=entry.metadata,
                )
                return

    def _find_ledger_by_stripe_id(self, stripe_pi_id: str) -> LedgerEntry | None:
        """Find a ledger entry by its Stripe PaymentIntent ID.

        Must be called with ``self._lock`` held.
        """
        for entry in self._ledger:
            if entry.stripe_payment_intent_id == stripe_pi_id:
                return entry
        return None
