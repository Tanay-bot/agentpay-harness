"""Stripe webhook signature generation and verification.

Implements the official Stripe webhook signing scheme:
Header format: t=<timestamp>,v1=<signature>[,v0=...]
Signed payload: <timestamp>.<raw_body>
Algorithm: HMAC-SHA256
"""

from __future__ import annotations

import hashlib
import hmac
import time

#: Signing secret the harness uses for every webhook it delivers to the agent.
#: Agents under test should verify ``StripeEvent.signature_header`` against it.
HARNESS_WEBHOOK_SECRET = "whsec_test_agentpay_local_secret_42"  # noqa: S105 - test-only secret


def compute_signature(payload: str | bytes, secret: str, timestamp: int) -> str:
    """Compute HMAC-SHA256 hex signature for timestamp.payload."""
    payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload

    signed_payload = f"{timestamp}.".encode() + payload_bytes
    secret_bytes = secret.encode("utf-8")

    return hmac.new(secret_bytes, signed_payload, hashlib.sha256).hexdigest()


def make_signature_header(timestamp: int, signature: str) -> str:
    """Format the Stripe-Signature header string."""
    return f"t={timestamp},v1={signature}"


def generate_test_signature(payload: str | bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate a complete Stripe-Signature header value for a given payload and secret."""
    ts = int(time.time()) if timestamp is None else timestamp
    sig = compute_signature(payload, secret, ts)
    return make_signature_header(ts, sig)


def verify_signature(
    payload: str | bytes,
    signature_header: str,
    secret: str,
    tolerance: int = 300,
    current_time: int | None = None,
) -> bool:
    """Verify a Stripe-Signature header against payload and secret.

    Parameters
    ----------
    payload:
        The raw request body.
    signature_header:
        The value of the 'Stripe-Signature' header.
    secret:
        The webhook signing secret (e.g. 'whsec_...').
    tolerance:
        Maximum allowed clock skew in seconds (default: 300s = 5 minutes).
    current_time:
        Optional unix timestamp override for deterministic testing.

    Returns
    -------
    True if signature and timestamp are valid, False otherwise.
    """
    if not signature_header or not secret:
        return False

    now = int(time.time()) if current_time is None else current_time

    # Parse header: t=12345,v1=abcde,v1=xyz
    timestamp: int | None = None
    signatures: list[str] = []

    for item in signature_header.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, val = item.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "t":
            try:
                timestamp = int(val)
            except ValueError:
                return False
        elif key == "v1":
            signatures.append(val)

    if timestamp is None or not signatures:
        return False

    # Check tolerance
    if tolerance > 0 and abs(now - timestamp) > tolerance:
        return False

    # Compute expected signature
    expected = compute_signature(payload, secret, timestamp)

    # Constant-time comparison against all v1 signatures in header
    return any(hmac.compare_digest(expected, s) for s in signatures)
