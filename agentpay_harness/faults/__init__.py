"""Faults — injectable failure modes for Stripe API interactions.

Each fault is a callable middleware that wraps the HTTP transport layer and
deterministically perturbs requests or responses according to a scenario's
fault specification.  Supported fault types:

- ``delay_response`` — add artificial latency to a matched endpoint.
- ``drop_response`` — silently discard the response (simulates network black-hole).
- ``error_response`` — return a synthetic Stripe error (e.g., 500, 402).
- ``duplicate_webhook`` — re-deliver a webhook event N times.
- ``reorder_webhooks`` — shuffle webhook delivery within a time window.
- ``replay_webhook`` — re-deliver a stale webhook event after a delay.
- ``partial_capture`` — return success but capture a different amount.
"""

from __future__ import annotations

__all__: list[str] = []
