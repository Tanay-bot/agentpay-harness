"""Ledger reconciliation — compare the agent's ledger with Stripe records.

Provides the ``reconcile`` function and the ``ReconciliationResult`` dataclass
that classify every payment into matched, missing, or mismatched buckets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agentpay_harness.types import (
    LedgerEntry,
    LedgerEntryStatus,
    RequestRecord,
    cents_to_usd,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconciliationResult:
    """Outcome of reconciling the agent's ledger against Stripe records.

    Attributes:
        matched: Pairs of ``(LedgerEntry, RequestRecord)`` where both sides
            agree on the payment intent ID and captured amount.
        missing_from_ledger: Stripe request records for captured payments
            that do *not* appear in the agent's ledger.
        missing_from_stripe: Ledger entries the agent claims are captured
            but no corresponding Stripe record exists.
        amount_mismatches: Tuples of
            ``(LedgerEntry, RequestRecord, ledger_amount, stripe_amount)``
            where the payment intent IDs match but amounts differ.
    """

    matched: list[tuple[LedgerEntry, RequestRecord]] = field(default_factory=list)
    missing_from_ledger: list[RequestRecord] = field(default_factory=list)
    missing_from_stripe: list[LedgerEntry] = field(default_factory=list)
    amount_mismatches: list[tuple[LedgerEntry, RequestRecord, int, int]] = field(
        default_factory=list
    )

    @property
    def is_clean(self) -> bool:
        """``True`` only when all records match and no mismatches exist."""
        return (
            not self.missing_from_ledger
            and not self.missing_from_stripe
            and not self.amount_mismatches
        )

    def summary(self) -> str:
        """Return a human-readable multi-line summary."""
        lines: list[str] = ["Ledger Reconciliation Summary", "=" * 40]

        lines.append(f"  Matched entries    : {len(self.matched)}")
        lines.append(f"  Missing from ledger: {len(self.missing_from_ledger)}")
        lines.append(f"  Missing from Stripe: {len(self.missing_from_stripe)}")
        lines.append(f"  Amount mismatches  : {len(self.amount_mismatches)}")
        lines.append(f"  Clean              : {'YES' if self.is_clean else 'NO'}")

        if self.missing_from_ledger:
            lines.append("")
            lines.append("Missing from agent ledger (Stripe has, agent doesn't):")
            for rec in self.missing_from_ledger:
                pi_id = _pi_id_from_record(rec)
                amount = _amount_from_record(rec)
                lines.append(f"  - PI {pi_id}: {cents_to_usd(amount)} USD")

        if self.missing_from_stripe:
            lines.append("")
            lines.append("Missing from Stripe (agent claims, Stripe doesn't have):")
            for entry in self.missing_from_stripe:
                lines.append(
                    f"  - PI {entry.stripe_payment_intent_id}: "
                    f"{cents_to_usd(entry.amount_cents)} USD"
                )

        if self.amount_mismatches:
            lines.append("")
            lines.append("Amount mismatches:")
            for _entry, rec, ledger_amt, stripe_amt in self.amount_mismatches:
                pi_id = _pi_id_from_record(rec)
                lines.append(
                    f"  - PI {pi_id}: ledger={cents_to_usd(ledger_amt)} USD, "
                    f"Stripe={cents_to_usd(stripe_amt)} USD "
                    f"(diff={cents_to_usd(abs(ledger_amt - stripe_amt))} USD)"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pi_id_from_record(rec: RequestRecord) -> str:
    """Extract the payment intent ID from a request record's response body."""
    if rec.response_body:
        return str(rec.response_body.get("id", "unknown"))
    return "unknown"


def _amount_from_record(rec: RequestRecord) -> int:
    """Extract the amount in cents from a request record's response body."""
    if rec.response_body:
        return int(rec.response_body.get("amount", 0))
    return 0


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------


def reconcile(
    agent_ledger: list[LedgerEntry],
    stripe_records: list[RequestRecord],
) -> ReconciliationResult:
    """Reconcile the agent's ledger entries against Stripe request records.

    Matching is performed on the ``stripe_payment_intent_id`` field.  Only
    ledger entries with status ``CAPTURED`` are considered (pending /
    failed / voided entries are not expected to appear in Stripe's balance).

    Args:
        agent_ledger: The agent's reported ledger snapshot.
        stripe_records: Request records for captured (successful) payment
            intents from the ``EventRecorder``.

    Returns:
        A ``ReconciliationResult`` classifying every entry.
    """
    result = ReconciliationResult()

    # Index ledger by PI id (only captured entries)
    ledger_by_pi: dict[str, LedgerEntry] = {}
    for entry in agent_ledger:
        if entry.status == LedgerEntryStatus.CAPTURED and entry.stripe_payment_intent_id:
            ledger_by_pi[entry.stripe_payment_intent_id] = entry

    # Index stripe records by PI id
    stripe_by_pi: dict[str, RequestRecord] = {}
    for rec in stripe_records:
        pi_id = _pi_id_from_record(rec)
        if pi_id and pi_id != "unknown":
            stripe_by_pi[pi_id] = rec

    # Find matches, mismatches, and missing entries
    all_pi_ids = set(ledger_by_pi) | set(stripe_by_pi)

    for pi_id in sorted(all_pi_ids):
        in_ledger = pi_id in ledger_by_pi
        in_stripe = pi_id in stripe_by_pi

        if in_ledger and in_stripe:
            entry = ledger_by_pi[pi_id]
            rec = stripe_by_pi[pi_id]
            stripe_amount = _amount_from_record(rec)
            if entry.amount_cents == stripe_amount:
                result.matched.append((entry, rec))
            else:
                result.amount_mismatches.append((entry, rec, entry.amount_cents, stripe_amount))
        elif in_stripe and not in_ledger:
            result.missing_from_ledger.append(stripe_by_pi[pi_id])
        elif in_ledger and not in_stripe:
            result.missing_from_stripe.append(ledger_by_pi[pi_id])

    logger.info(
        "Reconciliation complete: %d matched, %d missing from ledger, "
        "%d missing from Stripe, %d amount mismatches",
        len(result.matched),
        len(result.missing_from_ledger),
        len(result.missing_from_stripe),
        len(result.amount_mismatches),
    )
    return result
