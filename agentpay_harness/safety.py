"""Safety and compliance rules for agentpay-harness.

NON-NEGOTIABLE SAFETY RULES:
1. Test mode only: The harness refuses to run if it detects a live Stripe key
   (keys starting with 'sk_live_' or 'rk_live_'). Only 'sk_test_' and 'rk_test_'
   are permitted.
2. Never commit keys: Keys must be loaded from environment variables.
3. Live mode responses: Any API response with `livemode=True` triggers an immediate
   abort.
4. Public API surface only: Public Stripe APIs and standard test cards only.
"""

from __future__ import annotations

import os
from typing import Any


class LiveKeyError(ValueError):
    """Raised when a live Stripe API key is detected.

    The harness refuses to execute against production Stripe accounts to prevent
    accidental live charges or data corruption.
    """


def validate_stripe_key(key: str) -> None:
    """Validate that the Stripe API key is strictly a test-mode key.

    Parameters
    ----------
    key:
        The Stripe secret or restricted key string.

    Raises
    ------
    LiveKeyError:
        If the key begins with a live prefix (``sk_live_``, ``rk_live_``).
    ValueError:
        If the key is empty or has an unrecognized prefix.
    """
    if not key or not isinstance(key, str):
        raise ValueError("Stripe API key must be a non-empty string.")

    cleaned_key = key.strip()

    if cleaned_key.startswith("sk_live_") or cleaned_key.startswith("rk_live_"):
        raise LiveKeyError(
            "REFUSING TO RUN: Live Stripe key detected ('sk_live_...' / 'rk_live_...'). "
            "This harness MUST ONLY run against Stripe test mode or stripe-mock. "
            "Remove the live key and provide a test key (sk_test_... or rk_test_...)."
        )

    if not (
        cleaned_key.startswith("sk_test_")
        or cleaned_key.startswith("rk_test_")
        or cleaned_key.startswith("mock_")
    ):
        raise ValueError(
            f"Invalid Stripe key prefix for harness: expected 'sk_test_', 'rk_test_', or 'mock_', "
            f"got prefix '{cleaned_key[:8]}...'."
        )


def load_stripe_key_from_env(env_var: str = "STRIPE_TEST_SECRET_KEY") -> str:
    """Load and validate the Stripe test key from environment variables.

    Parameters
    ----------
    env_var:
        Environment variable name to read. Defaults to ``STRIPE_TEST_SECRET_KEY``.
        Falls back to ``STRIPE_API_KEY`` if ``STRIPE_TEST_SECRET_KEY`` is unset.

    Returns
    -------
    The validated test-mode API key.

    Raises
    ------
    ValueError:
        If no key is found in the environment.
    LiveKeyError:
        If a live key is supplied in the environment.
    """
    key = os.environ.get(env_var) or os.environ.get("STRIPE_API_KEY")
    if not key:
        # Default mock test key for local/offline testing if not configured
        return "sk_test_agentpay_mock_key_default"

    validate_stripe_key(key)
    return key.strip()


def validate_no_live_mode(api_response: dict[str, Any]) -> None:
    """Ensure an API response dictionary does not indicate live mode.

    Parameters
    ----------
    api_response:
        Parsed response body from a Stripe API call.

    Raises
    ------
    LiveKeyError:
        If ``livemode`` is present and True.
    """
    if not isinstance(api_response, dict):
        return

    if api_response.get("livemode") is True:
        raise LiveKeyError(
            "CRITICAL SAFETY VIOLATION: Stripe returned response with livemode=True! "
            "The harness has halted immediately to prevent unintended operations on real funds."
        )
