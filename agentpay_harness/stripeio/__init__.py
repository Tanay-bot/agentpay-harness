"""Stripe I/O — managed Stripe API client with fault-injection hooks.

This sub-package owns the HTTP connection to Stripe (or stripe-mock) and
exposes typed helpers for the subset of the Stripe API that payment agents
typically call:

- ``PaymentIntentClient`` — create, confirm, capture, cancel.
- ``CustomerClient`` — create, retrieve.
- ``RefundClient`` — create, retrieve.

All outbound requests pass through a middleware stack where registered
:mod:`agentpay_harness.faults` can intercept and mutate traffic.
"""

from __future__ import annotations

__all__: list[str] = []
