"""Built-in invariant checks I1 through I11.

Each check is a small, stateless class that implements the
``InvariantChecker`` protocol from :pymod:`agentpay_harness.invariants.checker`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    InvariantViolation,
    LedgerEntry,
    LedgerEntryStatus,
    RequestRecord,
    SessionContext,
    Severity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _successful_payment_intents(recorder: EventRecorder) -> list[RequestRecord]:
    """Return all *successful* create-payment-intent request records."""
    results: list[RequestRecord] = []
    for rec in recorder.requests:
        if (
            rec.method == "POST"
            and "/v1/payment_intents" in rec.path
            and "/capture" not in rec.path
            and "/cancel" not in rec.path
            and rec.response_body is not None
            and (
                (rec.response_status is not None and 200 <= rec.response_status < 300)
                or (
                    rec.fault_injected
                    and "drop" in rec.fault_injected.lower()
                    and rec.response_body.get("id")
                )
            )
        ):
            results.append(rec)
    return results


def _captured_payment_intents(recorder: EventRecorder) -> list[RequestRecord]:
    """Return request records whose response shows a captured payment intent."""
    results: list[RequestRecord] = []
    for rec in recorder.requests:
        if (
            rec.response_status is not None
            and 200 <= rec.response_status < 300
            and rec.response_body is not None
        ):
            body = rec.response_body
            obj_type = body.get("object")
            status = body.get("status")
            if obj_type == "payment_intent" and status == "succeeded":
                results.append(rec)
    return results


def _intent_id_from_metadata(rec: RequestRecord) -> str | None:
    """Extract the agent's intent_id from request metadata or response metadata."""
    # Check request body metadata
    if rec.body and isinstance(rec.body.get("metadata"), dict):
        iid = rec.body["metadata"].get("intent_id")
        if iid:
            return str(iid)
    # Fall back to response body metadata
    if rec.response_body and isinstance(rec.response_body.get("metadata"), dict):
        iid = rec.response_body["metadata"].get("intent_id")
        if iid:
            return str(iid)
    return None


# ---------------------------------------------------------------------------
# I1: At Most One Charge Per Intent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AtMostOneChargePerIntent:
    """I1 — No intent_id should produce more than one successful charge."""

    invariant_id: str = "I1"
    description: str = "At most one successful charge per intent"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        successes = _successful_payment_intents(recorder)

        by_intent: dict[str, list[RequestRecord]] = defaultdict(list)
        for rec in successes:
            iid = _intent_id_from_metadata(rec)
            if iid:
                by_intent[iid].append(rec)

        for intent_id, records in by_intent.items():
            distinct_pi_ids = list(
                dict.fromkeys(
                    r.response_body.get("id", "unknown")
                    for r in records
                    if r.response_body and r.response_body.get("id")
                )
            )
            if len(distinct_pi_ids) > 1:
                violations.append(
                    InvariantViolation(
                        invariant_id=self.invariant_id,
                        description=(
                            f"Intent {intent_id} produced {len(distinct_pi_ids)} "
                            f"distinct successful charges: {distinct_pi_ids}"
                        ),
                        severity=Severity.CRITICAL,
                        evidence={
                            "intent_id": intent_id,
                            "charge_count": len(distinct_pi_ids),
                            "payment_intent_ids": distinct_pi_ids,
                            "record_ids": [r.record_id for r in records],
                        },
                    )
                )
        return violations


# ---------------------------------------------------------------------------
# I2: Total Captured Within Budget
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TotalCapturedWithinBudget:
    """I2 — Sum of all captured amounts must not exceed the session budget."""

    invariant_id: str = "I2"
    description: str = "Total captured amount within budget"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        captured = _captured_payment_intents(recorder)
        total_cents = 0
        charge_details: list[dict[str, Any]] = []
        for rec in captured:
            if rec.response_body:
                amount = rec.response_body.get("amount", 0)
                total_cents += amount
                charge_details.append(
                    {
                        "payment_intent_id": rec.response_body.get("id"),
                        "amount_cents": amount,
                    }
                )

        if total_cents > session.budget_cents:
            return [
                InvariantViolation(
                    invariant_id=self.invariant_id,
                    description=(
                        f"Total captured {total_cents} cents exceeds "
                        f"budget of {session.budget_cents} cents"
                    ),
                    severity=Severity.CRITICAL,
                    evidence={
                        "total_captured_cents": total_cents,
                        "budget_cents": session.budget_cents,
                        "overage_cents": total_cents - session.budget_cents,
                        "charges": charge_details,
                    },
                )
            ]
        return []


# ---------------------------------------------------------------------------
# I3: Every Charge Has Approved Intent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EveryChargeHasApprovedIntent:
    """I3 — Every successful charge must map to exactly one ledger entry."""

    invariant_id: str = "I3"
    description: str = "Every charge has an approved intent in the ledger"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Build set of stripe PI ids from the ledger
        ledger_pi_ids: set[str] = set()
        for entry in ledger:
            if entry.stripe_payment_intent_id:
                ledger_pi_ids.add(entry.stripe_payment_intent_id)

        # Check every successful charge has a ledger entry
        captured = _captured_payment_intents(recorder)
        for rec in captured:
            if rec.response_body:
                pi_id = rec.response_body.get("id", "")
                if pi_id and pi_id not in ledger_pi_ids:
                    violations.append(
                        InvariantViolation(
                            invariant_id=self.invariant_id,
                            description=(
                                f"Stripe payment intent {pi_id} succeeded "
                                f"but has no matching ledger entry"
                            ),
                            severity=Severity.CRITICAL,
                            evidence={
                                "payment_intent_id": pi_id,
                                "amount_cents": rec.response_body.get("amount"),
                                "ledger_pi_ids": sorted(ledger_pi_ids),
                            },
                        )
                    )
        return violations


# ---------------------------------------------------------------------------
# I4: Idempotency Key Returns Original
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IdempotencyKeyReturnsOriginal:
    """I4 — Reused idempotency keys with identical params must return the same PI."""

    invariant_id: str = "I4"
    description: str = "Idempotency key reuse returns the original response"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Group requests by idempotency key
        by_key: dict[str, list[RequestRecord]] = defaultdict(list)
        for rec in recorder.requests:
            if rec.idempotency_key:
                by_key[rec.idempotency_key].append(rec)

        for key, records in by_key.items():
            if len(records) < 2:
                continue

            # Check if all have identical params (compare method + path + body)
            first = records[0]
            same_params = all(
                r.method == first.method and r.path == first.path and r.body == first.body
                for r in records[1:]
            )
            if not same_params:
                # Different params — handled by I5
                continue

            # All should return the same PI id
            pi_ids: set[str] = set()
            for r in records:
                if (
                    r.response_body
                    and r.response_status is not None
                    and 200 <= r.response_status < 300
                ):
                    pi_id = r.response_body.get("id")
                    if pi_id:
                        pi_ids.add(pi_id)

            if len(pi_ids) > 1:
                violations.append(
                    InvariantViolation(
                        invariant_id=self.invariant_id,
                        description=(
                            f"Idempotency key {key!r} returned different "
                            f"payment intent IDs: {sorted(pi_ids)}"
                        ),
                        severity=Severity.CRITICAL,
                        evidence={
                            "idempotency_key": key,
                            "payment_intent_ids": sorted(pi_ids),
                            "request_count": len(records),
                        },
                    )
                )
        return violations


# ---------------------------------------------------------------------------
# I5: Idempotency Key Mismatch Is Error
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IdempotencyKeyMismatchIsError:
    """I5 — Reused idempotency keys with *different* params must return 400."""

    invariant_id: str = "I5"
    description: str = "Idempotency key mismatch returns 400 error"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        by_key: dict[str, list[RequestRecord]] = defaultdict(list)
        for rec in recorder.requests:
            if rec.idempotency_key:
                by_key[rec.idempotency_key].append(rec)

        for key, records in by_key.items():
            if len(records) < 2:
                continue

            first = records[0]
            for later in records[1:]:
                params_differ = (
                    later.method != first.method
                    or later.path != first.path
                    or later.body != first.body
                )
                if params_differ:
                    # With different params, the response should be a 400
                    if later.response_status is not None and later.response_status != 400:
                        violations.append(
                            InvariantViolation(
                                invariant_id=self.invariant_id,
                                description=(
                                    f"Idempotency key {key!r} reused with "
                                    f"different params but got status "
                                    f"{later.response_status} instead of 400"
                                ),
                                severity=Severity.ERROR,
                                evidence={
                                    "idempotency_key": key,
                                    "first_request": {
                                        "method": first.method,
                                        "path": first.path,
                                        "body": first.body,
                                    },
                                    "later_request": {
                                        "method": later.method,
                                        "path": later.path,
                                        "body": later.body,
                                    },
                                    "response_status": later.response_status,
                                },
                            )
                        )
        return violations


# ---------------------------------------------------------------------------
# I6: Webhook Event Processed At Most Once
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WebhookEventProcessedAtMostOnce:
    """I6 — No event_id should cause duplicate side-effects in the ledger."""

    invariant_id: str = "I6"
    description: str = "Webhook events processed at most once"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Look for duplicate ledger entries triggered by the same event
        # The convention is that metadata["event_id"] records the causal event.
        by_event: dict[str, list[LedgerEntry]] = defaultdict(list)
        for entry in ledger:
            event_id = entry.metadata.get("event_id")
            if event_id:
                by_event[event_id].append(entry)

        for event_id, entries in by_event.items():
            if len(entries) > 1:
                violations.append(
                    InvariantViolation(
                        invariant_id=self.invariant_id,
                        description=(
                            f"Event {event_id} caused {len(entries)} "
                            f"ledger entries (expected at most 1)"
                        ),
                        severity=Severity.CRITICAL,
                        evidence={
                            "event_id": event_id,
                            "duplicate_count": len(entries),
                            "entry_ids": [e.entry_id for e in entries],
                            "amounts": [e.amount_cents for e in entries],
                        },
                    )
                )
        return violations


# ---------------------------------------------------------------------------
# I7: Event Processing Order Independent
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EventProcessingOrderIndependent:
    """I7 — Terminal state must be correct regardless of event arrival order.

    Verifies that if the recorder shows events delivered out-of-order,
    the final ledger state is still consistent with the terminal Stripe
    state for each payment intent.
    """

    invariant_id: str = "I7"
    description: str = "Event processing is order-independent"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Determine the terminal Stripe status for each PI from requests
        terminal_status: dict[str, str] = {}
        terminal_amount: dict[str, int] = {}
        for rec in recorder.requests:
            if rec.response_body and rec.response_status and 200 <= rec.response_status < 300:
                body = rec.response_body
                if body.get("object") == "payment_intent":
                    pi_id = body.get("id", "")
                    status = body.get("status", "")
                    amount = body.get("amount", 0)
                    if pi_id:
                        terminal_status[pi_id] = status
                        terminal_amount[pi_id] = amount

        # Build ledger index by PI id
        ledger_by_pi: dict[str, LedgerEntry] = {}
        for entry in ledger:
            if entry.stripe_payment_intent_id:
                # Keep the last entry (most recent state)
                ledger_by_pi[entry.stripe_payment_intent_id] = entry

        # Verify consistency: the ledger's status should match the terminal status
        status_map: dict[str, set[str]] = {
            "succeeded": {LedgerEntryStatus.CAPTURED.value},
            "canceled": {LedgerEntryStatus.VOIDED.value, LedgerEntryStatus.FAILED.value},
            "requires_payment_method": {LedgerEntryStatus.FAILED.value},
        }

        for pi_id, stripe_status in terminal_status.items():
            if pi_id not in ledger_by_pi:
                continue  # Handled by I3
            entry = ledger_by_pi[pi_id]
            expected_statuses = status_map.get(stripe_status)
            if expected_statuses and entry.status.value not in expected_statuses:
                violations.append(
                    InvariantViolation(
                        invariant_id=self.invariant_id,
                        description=(
                            f"Payment intent {pi_id} has terminal Stripe status "
                            f"{stripe_status!r} but ledger shows "
                            f"{entry.status.value!r}"
                        ),
                        severity=Severity.ERROR,
                        evidence={
                            "payment_intent_id": pi_id,
                            "stripe_terminal_status": stripe_status,
                            "ledger_status": entry.status.value,
                            "expected_ledger_statuses": sorted(expected_statuses)
                            if expected_statuses
                            else [],
                        },
                    )
                )

        return violations


# ---------------------------------------------------------------------------
# I8: Webhook Signatures Verified
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WebhookSignaturesVerified:
    """I8 — Agent must not process events with invalid signatures.

    Checks whether the recorder contains webhook deliveries flagged as
    having invalid or expired signatures, and whether the agent still
    processed them (i.e. the event appears in the ledger).
    """

    invariant_id: str = "I8"
    description: str = "Webhook signatures verified before processing"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Collect event IDs that had invalid signatures from the timeline
        invalid_sig_event_ids: set[str] = set()
        for entry in recorder.timeline:
            if entry.entry_type == "webhook_delivery":
                meta = entry.metadata or {}
                if meta.get("signature_valid") is False:
                    evt_id = meta.get("event_id")
                    if evt_id:
                        invalid_sig_event_ids.add(evt_id)

        if not invalid_sig_event_ids:
            return violations

        # Check if any of these event IDs appear in the ledger
        ledger_event_ids: set[str] = set()
        for le in ledger:
            eid = le.metadata.get("event_id")
            if eid:
                ledger_event_ids.add(eid)

        processed_invalid = invalid_sig_event_ids & ledger_event_ids
        for evt_id in sorted(processed_invalid):
            violations.append(
                InvariantViolation(
                    invariant_id=self.invariant_id,
                    description=(
                        f"Event {evt_id} had an invalid/expired signature "
                        f"but was still processed by the agent"
                    ),
                    severity=Severity.CRITICAL,
                    evidence={
                        "event_id": evt_id,
                        "reason": "invalid_signature",
                    },
                )
            )
        return violations


# ---------------------------------------------------------------------------
# I9: Abort Cleans Up
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AbortCleansUp:
    """I9 — After the run, no PI should be in requires_capture and no unrefunded charges."""

    invariant_id: str = "I9"
    description: str = "Abort cleans up (no dangling captures or unrefunded charges)"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Build a map of PI id → latest known status from request records
        pi_state: dict[str, dict[str, Any]] = {}
        for rec in recorder.requests:
            if (
                rec.response_body
                and rec.response_status is not None
                and 200 <= rec.response_status < 300
                and rec.response_body.get("object") == "payment_intent"
            ):
                pi_id = rec.response_body.get("id", "")
                if pi_id:
                    pi_state[pi_id] = rec.response_body

        # Check for PIs still in requires_capture
        for pi_id, state in pi_state.items():
            status = state.get("status", "")
            if status == "requires_capture":
                violations.append(
                    InvariantViolation(
                        invariant_id=self.invariant_id,
                        description=(
                            f"Payment intent {pi_id} is still in "
                            f"'requires_capture' after run completed"
                        ),
                        severity=Severity.ERROR,
                        evidence={
                            "payment_intent_id": pi_id,
                            "status": status,
                            "amount_cents": state.get("amount"),
                        },
                    )
                )

            # Check for succeeded PIs where charges are not refunded
            # (only relevant if the PI is not in a terminal good state)
            charges = state.get("charges", {})
            if isinstance(charges, dict):
                charge_list = charges.get("data", [])
            elif isinstance(charges, list):
                charge_list = charges
            else:
                charge_list = []

            for charge in charge_list:
                if isinstance(charge, dict):
                    if charge.get("captured") and not charge.get("refunded"):
                        # Only flag if the PI itself was not intentionally captured
                        if status not in ("succeeded", "canceled"):
                            violations.append(
                                InvariantViolation(
                                    invariant_id=self.invariant_id,
                                    description=(
                                        f"Charge {charge.get('id', 'unknown')} on "
                                        f"PI {pi_id} is captured but not refunded "
                                        f"(PI status: {status})"
                                    ),
                                    severity=Severity.ERROR,
                                    evidence={
                                        "payment_intent_id": pi_id,
                                        "charge_id": charge.get("id"),
                                        "pi_status": status,
                                        "captured": charge.get("captured"),
                                        "refunded": charge.get("refunded"),
                                    },
                                )
                            )
        return violations


# ---------------------------------------------------------------------------
# I10: Rate Limit Backoff
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RateLimitBackoff:
    """I10 — When 429s are injected, retries must show exponential backoff (≥1.5x)."""

    invariant_id: str = "I10"
    description: str = "Rate-limit retries use exponential backoff"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []

        # Find all 429 responses and group by endpoint (method + path)
        rate_limited: dict[str, list[RequestRecord]] = defaultdict(list)
        for rec in recorder.requests:
            if rec.response_status == 429:
                endpoint = f"{rec.method} {rec.path}"
                rate_limited[endpoint].append(rec)

        if not rate_limited:
            return violations

        # For each endpoint, get all requests (429s and subsequent retries)
        for endpoint in rate_limited:
            # Get all requests to this endpoint, sorted by timestamp
            all_reqs = sorted(
                [r for r in recorder.requests if f"{r.method} {r.path}" == endpoint],
                key=lambda r: r.timestamp,
            )

            if len(all_reqs) < 2:
                continue

            # Compute gaps between consecutive requests
            gaps: list[float] = []
            for i in range(1, len(all_reqs)):
                gap = (all_reqs[i].timestamp - all_reqs[i - 1].timestamp).total_seconds()
                gaps.append(gap)

            # Check that gaps are increasing by at least 1.5x
            for i in range(1, len(gaps)):
                if gaps[i - 1] > 0 and gaps[i] < gaps[i - 1] * 1.5:
                    violations.append(
                        InvariantViolation(
                            invariant_id=self.invariant_id,
                            description=(
                                f"Retry gap on {endpoint} did not increase "
                                f"by ≥1.5x: gap[{i - 1}]={gaps[i - 1]:.3f}s, "
                                f"gap[{i}]={gaps[i]:.3f}s"
                            ),
                            severity=Severity.WARNING,
                            evidence={
                                "endpoint": endpoint,
                                "gap_index": i,
                                "previous_gap_s": round(gaps[i - 1], 4),
                                "current_gap_s": round(gaps[i], 4),
                                "ratio": round(gaps[i] / gaps[i - 1], 4) if gaps[i - 1] > 0 else 0,
                                "all_gaps_s": [round(g, 4) for g in gaps],
                                "total_requests": len(all_reqs),
                            },
                        )
                    )
                    break  # One violation per endpoint is enough
        return violations


# ---------------------------------------------------------------------------
# I11: Ledger Reconciles With Stripe
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LedgerReconcilesWithStripe:
    """I11 — Agent's ledger must match Stripe's actual balance transactions."""

    invariant_id: str = "I11"
    description: str = "Ledger reconciles with Stripe records"

    def check(
        self,
        recorder: EventRecorder,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        from agentpay_harness.ledger import reconcile

        stripe_records = _captured_payment_intents(recorder)
        result = reconcile(ledger, stripe_records)

        violations: list[InvariantViolation] = []

        for rec in result.missing_from_ledger:
            pi_id = rec.response_body.get("id", "unknown") if rec.response_body else "unknown"
            amount = rec.response_body.get("amount", 0) if rec.response_body else 0
            violations.append(
                InvariantViolation(
                    invariant_id=self.invariant_id,
                    description=(
                        f"Stripe has captured PI {pi_id} ({amount} cents) "
                        f"but agent's ledger does not"
                    ),
                    severity=Severity.CRITICAL,
                    evidence={
                        "payment_intent_id": pi_id,
                        "stripe_amount_cents": amount,
                        "direction": "missing_from_ledger",
                    },
                )
            )

        for entry in result.missing_from_stripe:
            violations.append(
                InvariantViolation(
                    invariant_id=self.invariant_id,
                    description=(
                        f"Agent ledger claims PI {entry.stripe_payment_intent_id} "
                        f"({entry.amount_cents} cents) but Stripe has no such capture"
                    ),
                    severity=Severity.CRITICAL,
                    evidence={
                        "entry_id": entry.entry_id,
                        "payment_intent_id": entry.stripe_payment_intent_id,
                        "ledger_amount_cents": entry.amount_cents,
                        "direction": "missing_from_stripe",
                    },
                )
            )

        for _entry, rec, ledger_amt, stripe_amt in result.amount_mismatches:
            pi_id = rec.response_body.get("id", "unknown") if rec.response_body else "unknown"
            violations.append(
                InvariantViolation(
                    invariant_id=self.invariant_id,
                    description=(
                        f"Amount mismatch for PI {pi_id}: "
                        f"ledger={ledger_amt} cents, Stripe={stripe_amt} cents"
                    ),
                    severity=Severity.ERROR,
                    evidence={
                        "payment_intent_id": pi_id,
                        "ledger_amount_cents": ledger_amt,
                        "stripe_amount_cents": stripe_amt,
                        "difference_cents": abs(ledger_amt - stripe_amt),
                    },
                )
            )

        return violations
