"""Invariants — post-run correctness checks for payment safety properties.

Each invariant receives the recorded requests, webhook deliveries and the
agent's ledger from a scenario run and returns a list of violations.

========  ===================================================================
ID        Description
========  ===================================================================
I1        At most one successful charge per intent.
I2        Total captured amount within budget.
I3        Every charge has an approved intent in the ledger.
I4        Idempotency key reuse returns the original response.
I5        Idempotency key mismatch returns a 400 error.
I6        Webhook events processed at most once.
I7        Event processing is order-independent.
I8        Webhook signatures (and timestamps) verified before processing.
I9        Abort cleans up (no dangling authorizations).
I10       Rate-limit retries use exponential backoff.
I11       Ledger reconciles with Stripe records.
I12       Ledger records the captured amount, not the authorized amount.
I13       Cumulative refunds never exceed the captured amount.
========  ===================================================================
"""

from __future__ import annotations

__all__: list[str] = []
