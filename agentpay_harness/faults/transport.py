"""Transport-level fault implementations.

Each function returns a :class:`FaultAction` (defined in
:mod:`agentpay_harness.faults.engine`) that describes the fault to apply at the
HTTP-transport level.  The :class:`RecordingStripeClient` interprets these
actions before or instead of making the real Stripe API call.
"""

from __future__ import annotations

import fnmatch
import random

from agentpay_harness.faults.actions import FaultAction, FaultActionType
from agentpay_harness.types import RequestRecord

# ======================================================================
# Transport fault factories
# ======================================================================


def drop_response_action(request_record: RequestRecord | None = None) -> FaultAction:
    """Return a :class:`FaultAction` simulating a dropped/timed-out response.

    Parameters
    ----------
    request_record:
        Optional request record for context (unused but kept for API symmetry).
    """
    return FaultAction(
        action_type=FaultActionType.DROP_RESPONSE,
        response_override={
            "error": {
                "type": "api_connection_error",
                "message": "Request timed out (fault injection)",
            },
        },
        fault_description="Dropped response — simulated timeout",
    )


# Keep the name ``drop_response`` as an alias expected by the spec.
drop_response = drop_response_action


def inject_429() -> FaultAction:
    """Return a :class:`FaultAction` simulating a Stripe 429 rate-limit response.

    Includes a ``Retry-After`` header value in the response override.
    """
    return FaultAction(
        action_type=FaultActionType.INJECT_429,
        response_override={
            "error": {
                "type": "rate_limit_error",
                "message": "Too many requests (fault injection)",
                "code": "rate_limit",
            },
            "headers": {"Retry-After": "1"},
            "status": 429,
        },
        fault_description="Injected 429 — rate limit exceeded",
    )


def inject_5xx(seed: random.Random) -> FaultAction:
    """Return a :class:`FaultAction` simulating a 500 or 503 server error.

    Parameters
    ----------
    seed:
        A :class:`random.Random` instance used to choose between 500 and 503.
    """
    status = seed.choice([500, 503])
    message = (
        "Internal server error (fault injection)"
        if status == 500
        else "Service unavailable (fault injection)"
    )
    return FaultAction(
        action_type=FaultActionType.INJECT_5XX,
        response_override={
            "error": {
                "type": "api_error",
                "message": message,
            },
            "status": status,
        },
        fault_description=f"Injected {status} — server error",
    )


def delay_response(ms: int) -> FaultAction:
    """Return a :class:`FaultAction` that adds an artificial delay.

    Parameters
    ----------
    ms:
        Delay in milliseconds.  The client will ``time.sleep`` for this
        duration before making the real API call.
    """
    return FaultAction(
        action_type=FaultActionType.DELAY,
        delay_ms=ms,
        fault_description=f"Delayed response by {ms}ms",
    )


def duplicate_request() -> FaultAction:
    """Return a :class:`FaultAction` flagging that the request should be sent twice.

    The client will execute the real Stripe API call twice, recording both
    responses, but only returning the first to the caller.
    """
    return FaultAction(
        action_type=FaultActionType.DUPLICATE,
        fault_description="Duplicate request — sent twice",
    )


# ======================================================================
# Pattern matching helper
# ======================================================================


def match_request(pattern: str, method: str, path: str) -> bool:
    """Match a fault-rule pattern against an actual HTTP method and path.

    Patterns look like::

        POST /v1/payment_intents
        POST /v1/payment_intents/*/capture
        GET /v1/balance_transactions
        * /v1/payment_intents/*

    Rules:
    - The pattern is split into ``<method_pattern> <path_pattern>``.
    - ``*`` in the method position matches any HTTP method.
    - ``*`` segments in the path are matched as single path-component globs.
    - If the pattern has no space, it is treated as a path-only pattern
      matching any method.

    Parameters
    ----------
    pattern:
        The fault-rule pattern string.
    method:
        The actual HTTP method (e.g. ``"POST"``).
    path:
        The actual request path (e.g. ``"/v1/payment_intents/pi_123/capture"``).
    """
    pattern = pattern.strip()
    if not pattern:
        return False

    parts = pattern.split(None, 1)
    if len(parts) == 2:
        method_pat, path_pat = parts
    else:
        # No method component — match any method.
        method_pat = "*"
        path_pat = parts[0]

    # Method match (case-insensitive).
    if method_pat != "*" and method_pat.upper() != method.upper():
        return False

    # Path match using fnmatch-style globbing.
    return fnmatch.fnmatch(path, path_pat)
