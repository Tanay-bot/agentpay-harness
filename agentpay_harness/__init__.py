"""agentpay-harness — Fault-injection test harness for AI-agent-initiated Stripe payments."""

from __future__ import annotations

from agentpay_harness.adapters.protocol import AgentUnderTest, StripeClient
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.safety import LiveKeyError, validate_stripe_key
from agentpay_harness.scenario import load_scenario
from agentpay_harness.shrink import shrink
from agentpay_harness.types import (
    Intent,
    InvariantViolation,
    PaymentResult,
    RequestRecord,
    RunResult,
    Task,
)

__version__ = "0.1.0"
__all__ = [
    "DEFAULT_REGISTRY",
    "AgentUnderTest",
    "HarnessDriver",
    "Intent",
    "InvariantViolation",
    "LiveKeyError",
    "PaymentResult",
    "ReferenceAgent",
    "RequestRecord",
    "RunResult",
    "StripeClient",
    "Task",
    "__version__",
    "load_scenario",
    "shrink",
    "validate_stripe_key",
]
