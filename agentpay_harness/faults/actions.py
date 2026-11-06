"""Fault and webhook action descriptors for the fault engine.

Separated from engine.py to avoid circular dependencies between the engine
and specific fault implementations in transport.py and webhook.py.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from agentpay_harness.types import StripeEvent


class FaultActionType(enum.Enum):
    """Discriminator for :class:`FaultAction`."""

    PASS_THROUGH = "pass_through"  # noqa: S105 - enum value, not a secret
    DROP_RESPONSE = "drop_response"
    INJECT_429 = "inject_429"
    INJECT_5XX = "inject_5xx"
    DELAY = "delay"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class FaultAction:
    """Describes the fault to apply to a single API request.

    Uses a discriminated-union style: inspect ``action_type`` to determine
    which optional fields are populated.
    """

    action_type: FaultActionType
    delay_ms: int | None = None
    response_override: dict[str, Any] | None = None
    fault_description: str = ""


class WebhookActionType(enum.Enum):
    """Discriminator for :class:`WebhookAction`."""

    DELIVER = "deliver"
    DUPLICATE = "duplicate"
    DELAY = "delay"
    DROP = "drop"
    REORDER = "reorder"


@dataclass(frozen=True, slots=True)
class WebhookAction:
    """Describes a fault-modified webhook delivery."""

    action_type: WebhookActionType
    event: StripeEvent
    delay_ms: int = 0
    fault_description: str = ""


PASS_THROUGH = FaultAction(
    action_type=FaultActionType.PASS_THROUGH,
    fault_description="No fault — pass through",
)
