"""Invariants — post-run correctness checks for payment safety properties.

Each invariant is a pure function that receives the full event log from a
scenario run and returns a pass/fail verdict with a human-readable explanation.

Invariant catalog
-----------------
========  ===================================================================
ID        Description
========  ===================================================================
I1        Every mutating Stripe API call carries an idempotency key.
I2        Agent checks remaining budget before initiating any charge.
I3        Budget decrement is atomic (compare-and-swap, not read-then-write).
I4        Total captured amount ≤ authorised budget.
I5        Every PaymentIntent reaches a terminal state within the timeout.
I6        Duplicate webhook events do not cause duplicate side-effects.
I7        Agent does not assume webhook delivery order.
I8        Stale webhook replays beyond the tolerance window are discarded.
I9        No duplicate PaymentIntents for the same logical operation.
I10       Refund amount ≤ captured amount for every PaymentIntent.
I11       Budget mutations are serialised (no concurrent modifications).
========  ===================================================================
"""

from __future__ import annotations

__all__: list[str] = []
