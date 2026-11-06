"""Webhooks — local webhook receiver with fault-injection support.

Provides:

- ``WebhookServer`` — a lightweight ASGI server that listens for Stripe
  webhook events, verifies signatures, and dispatches them to registered
  handlers.
- ``WebhookFaultProxy`` — sits in front of the server and applies webhook
  faults (duplication, reordering, replay) before events reach handlers.
"""

from __future__ import annotations

__all__: list[str] = []
