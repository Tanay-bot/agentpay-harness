"""Webhook fault implementations.

These functions produce lists of :class:`WebhookAction` objects that describe
how the :class:`WebhookDeliverySystem` should (mis)deliver webhook events to
simulate real-world Stripe delivery anomalies.
"""

from __future__ import annotations

import random

from agentpay_harness.faults.actions import WebhookAction, WebhookActionType
from agentpay_harness.types import StripeEvent


def duplicate_webhook(event: StripeEvent, times: int = 2) -> list[WebhookAction]:
    """Produce ``times`` deliveries of the same event.

    Stripe may deliver the same event more than once (at-least-once
    semantics).  This helper generates the duplicate actions.

    Parameters
    ----------
    event:
        The event to duplicate.
    times:
        Total number of deliveries (original + duplicates).  Must be ≥ 1.
    """
    times = max(1, times)
    actions: list[WebhookAction] = []
    for i in range(times):
        desc = "Original delivery" if i == 0 else f"Duplicate delivery #{i}"
        actions.append(
            WebhookAction(
                action_type=WebhookActionType.DUPLICATE if i > 0 else WebhookActionType.DELIVER,
                event=event,
                delay_ms=0,
                fault_description=desc,
            )
        )
    return actions


def reorder_webhooks(
    events: list[StripeEvent],
    window: int,
    rng: random.Random,
) -> list[WebhookAction]:
    """Shuffle events within a sliding window to simulate out-of-order delivery.

    Stripe does **not** guarantee delivery order.  This helper reorders events
    within a window of ``window`` events using the provided RNG for
    deterministic shuffling.

    Parameters
    ----------
    events:
        The list of events to reorder.
    window:
        The size of the sliding window.  Events within each window are shuffled.
    rng:
        A seeded :class:`random.Random` for deterministic behaviour.
    """
    if not events:
        return []

    reordered: list[StripeEvent] = []
    for start in range(0, len(events), max(1, window)):
        chunk = list(events[start : start + window])
        rng.shuffle(chunk)
        reordered.extend(chunk)

    actions: list[WebhookAction] = []
    for idx, evt in enumerate(reordered):
        actions.append(
            WebhookAction(
                action_type=WebhookActionType.REORDER,
                event=evt,
                delay_ms=idx * 50,  # small staggered delay to enforce ordering
                fault_description=f"Reordered delivery (position {idx})",
            )
        )
    return actions


def replay_webhook(event: StripeEvent, delay_s: int = 30) -> list[WebhookAction]:
    """Deliver an event, then deliver it again after a delay.

    Simulates Stripe retrying delivery after the endpoint returned an error
    or timed out.

    Parameters
    ----------
    event:
        The event to replay.
    delay_s:
        Delay in **seconds** before the replay delivery.
    """
    return [
        WebhookAction(
            action_type=WebhookActionType.DELIVER,
            event=event,
            delay_ms=0,
            fault_description="Original delivery",
        ),
        WebhookAction(
            action_type=WebhookActionType.DUPLICATE,
            event=event,
            delay_ms=delay_s * 1000,
            fault_description=f"Replayed delivery after {delay_s}s",
        ),
    ]


def delay_webhook(event: StripeEvent, ms: int = 5000) -> list[WebhookAction]:
    """Deliver an event after an artificial delay.

    Parameters
    ----------
    event:
        The event to delay.
    ms:
        Delay in milliseconds.
    """
    return [
        WebhookAction(
            action_type=WebhookActionType.DELAY,
            event=event,
            delay_ms=ms,
            fault_description=f"Delayed delivery by {ms}ms",
        ),
    ]
