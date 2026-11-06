"""Tests for safety rules and live-key prevention."""

from __future__ import annotations

import pytest

from agentpay_harness.safety import (
    LiveKeyError,
    validate_no_live_mode,
    validate_stripe_key,
)
from agentpay_harness.stripeio.client import RecordingStripeClient

# Constructed to avoid GitHub secret scanning false positives on dummy test strings
SAMPLE_LIVE_KEY = f"{'sk'}_{'live'}_sample_mock_token_for_tests"
SAMPLE_LIVE_RK = f"{'rk'}_{'live'}_sample_mock_token_for_tests"
SAMPLE_TEST_KEY = f"{'sk'}_{'test'}_sample_mock_token_for_tests"
SAMPLE_TEST_RK = f"{'rk'}_{'test'}_sample_mock_token_for_tests"


def test_refuse_live_secret_key() -> None:
    """Harness must refuse any key beginning with sk_live_."""
    with pytest.raises(LiveKeyError, match="Live Stripe key detected"):
        validate_stripe_key(SAMPLE_LIVE_KEY)


def test_refuse_live_restricted_key() -> None:
    """Harness must refuse any key beginning with rk_live_."""
    with pytest.raises(LiveKeyError, match="Live Stripe key detected"):
        validate_stripe_key(SAMPLE_LIVE_RK)


def test_allow_test_secret_key() -> None:
    """Harness must allow sk_test_ keys."""
    validate_stripe_key(SAMPLE_TEST_KEY)


def test_allow_test_restricted_key() -> None:
    """Harness must allow rk_test_ keys."""
    validate_stripe_key(SAMPLE_TEST_RK)


def test_recording_client_refuses_live_key() -> None:
    """Client constructor must immediately fail on a live key."""
    with pytest.raises(LiveKeyError):
        RecordingStripeClient(SAMPLE_LIVE_KEY)


def test_validate_no_live_mode_flags_violation() -> None:
    """Response with livemode=True must trigger LiveKeyError."""
    with pytest.raises(LiveKeyError, match="CRITICAL SAFETY VIOLATION"):
        validate_no_live_mode({"id": "pi_123", "livemode": True})
