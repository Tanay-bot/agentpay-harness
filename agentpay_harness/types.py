"""Core domain types for agentpay-harness.

Every type is a frozen dataclass with slots enabled for memory efficiency and
immutability guarantees.  Mutable defaults use ``field(default_factory=...)``.
Auto-generated identifiers use ``uuid4().hex`` for compactness.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PaymentStatus(enum.Enum):
    """Outcome status of a payment attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUIRES_ACTION = "requires_action"
    CANCELED = "canceled"
    PROCESSING = "processing"
    TIMEOUT = "timeout"


class LedgerEntryStatus(enum.Enum):
    """Lifecycle status of a ledger entry."""

    PENDING = "pending"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    VOIDED = "voided"
    FAILED = "failed"


class FaultType(enum.Enum):
    """Types of faults the harness can inject."""

    DROP_RESPONSE = "drop_response"
    DUPLICATE_REQUEST = "duplicate_request"
    DUPLICATE_WEBHOOK = "duplicate_webhook"
    REORDER_WEBHOOKS = "reorder_webhooks"
    REPLAY_WEBHOOK = "replay_webhook"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    DELAY_RESPONSE = "delay_response"
    CLOCK_SKEW = "clock_skew"
    FORGED_WEBHOOK = "forged_webhook"
    ASYNC_PAYMENT_FAILURE = "async_payment_failure"


class Severity(enum.Enum):
    """Severity level for invariant violations."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


# ---------------------------------------------------------------------------
# Core domain dataclasses
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=UTC)


def _new_id() -> str:
    """Generate a compact hex UUID."""
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class Task:
    """A high-level task assigned to an agent.

    Attributes:
        task_id: Unique identifier (auto-generated).
        description: Human-readable description of what the agent should do.
        budget_cents: Maximum spend budget in cents.
        metadata: Arbitrary key-value pairs for caller use.
    """

    description: str
    budget_cents: int
    task_id: str = field(default_factory=_new_id)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.budget_cents < 0:
            raise ValueError(f"budget_cents must be non-negative, got {self.budget_cents}")
        if not self.description:
            raise ValueError("description must not be empty")


@dataclass(frozen=True, slots=True)
class Intent:
    """A single payment intent planned by an agent.

    Attributes:
        intent_id: Unique identifier (auto-generated).
        task_id: Parent task this intent belongs to.
        amount_cents: Amount to charge in the smallest currency unit.
        currency: ISO 4217 currency code (lowercase).
        description: What this payment is for.
        idempotency_key: Caller-supplied idempotency key for Stripe API calls.
        metadata: Arbitrary key-value pairs.
        created_at: Timestamp when this intent was created.
    """

    task_id: str
    amount_cents: int
    description: str
    intent_id: str = field(default_factory=_new_id)
    currency: str = "usd"
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    capture_method: str = "automatic"

    def __post_init__(self) -> None:
        if self.amount_cents <= 0:
            raise ValueError(f"amount_cents must be positive, got {self.amount_cents}")
        if not self.currency:
            raise ValueError("currency must not be empty")
        if not self.description:
            raise ValueError("description must not be empty")


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Result of executing a payment intent against Stripe.

    Attributes:
        intent_id: The local intent this result corresponds to.
        stripe_payment_intent_id: Stripe's ``pi_...`` identifier, if created.
        status: Outcome status.
        amount_cents: Amount that was charged (or attempted).
        error: Human-readable error message on failure.
        idempotency_key: The idempotency key used for this request.
        request_id: Stripe's ``req_...`` request identifier.
        created_at: Timestamp of the result.
    """

    intent_id: str
    status: PaymentStatus
    amount_cents: int
    stripe_payment_intent_id: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    request_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """An entry in the agent's internal payment ledger.

    Attributes:
        entry_id: Unique identifier (auto-generated).
        intent_id: The local intent this entry records.
        stripe_payment_intent_id: Stripe's ``pi_...`` identifier.
        amount_cents: Amount in smallest currency unit.
        currency: ISO 4217 currency code.
        status: Current ledger status.
        description: What this charge was for.
        created_at: Timestamp of creation.
        metadata: Arbitrary key-value pairs.
    """

    intent_id: str
    amount_cents: int
    currency: str
    status: LedgerEntryStatus
    description: str
    entry_id: str = field(default_factory=_new_id)
    stripe_payment_intent_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StripeEvent:
    """A Stripe webhook event.

    Attributes:
        event_id: Stripe's ``evt_...`` identifier.
        type: Event type string (e.g. ``payment_intent.succeeded``).
        data: Event data payload.
        created: Unix timestamp of event creation.
        livemode: Whether this event is from live mode.
        pending_webhooks: Number of pending webhook deliveries.
        api_version: Stripe API version for this event.
        payload: Raw JSON body as delivered (set by the harness's delivery system).
        signature_header: ``Stripe-Signature`` header value for ``payload``.  Agents
            should verify it with :func:`agentpay_harness.webhooks.signatures.verify_signature`.
    """

    event_id: str
    type: str
    data: dict[str, Any]
    created: int
    livemode: bool = False
    pending_webhooks: int = 0
    api_version: str | None = None
    payload: bytes | None = field(default=None, compare=False, repr=False)
    signature_header: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.livemode:
            raise ValueError("StripeEvent must not be in livemode within the harness")
        if self.created < 0:
            raise ValueError(f"created timestamp must be non-negative, got {self.created}")

    @property
    def event_type(self) -> str:
        """Alias for type (e.g. payment_intent.succeeded)."""
        return self.type

    @property
    def timestamp(self) -> datetime:
        """Return datetime representation of unix created timestamp."""
        return datetime.fromtimestamp(self.created, tz=UTC)


# ---------------------------------------------------------------------------
# Fault & scenario configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaultConfig:
    """Configuration for a single fault to inject.

    Attributes:
        type: The kind of fault.
        on: HTTP method + path pattern to match (e.g. ``POST /v1/payment_intents``).
        event: Stripe event type to target (for webhook faults).
        occurrence: Which occurrence to inject on (1-indexed).
        times: How many times to inject.
        ms: Milliseconds parameter (e.g. delay duration).
        window: Window size in seconds (e.g. for reordering).
        delay_s: Delay in seconds (e.g. for replay).
    """

    type: FaultType
    on: str | None = None
    event: str | None = None
    occurrence: int | None = None
    times: int | None = None
    ms: int | None = None
    window: int | None = None
    delay_s: int | None = None


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    """Configuration for a complete test scenario.

    Attributes:
        name: Human-readable scenario name.
        budget_usd: Total spend budget as a Decimal in USD.
        task: Description of the task to assign.
        faults: List of faults to inject during the run.
        seed: Random seed for reproducibility.
        assertions: List of invariant IDs to check (e.g. ``["I1", "I4"]``).
        concurrency: Number of concurrent agent executions.
        capture_method: ``automatic`` or ``manual`` (authorize now, capture later).
        abort: If true, the driver calls the agent's ``abort(client)`` after paying.
    """

    name: str
    budget_usd: Decimal
    task: str
    seed: int
    faults: list[FaultConfig] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)
    concurrency: int = 1
    capture_method: str = "automatic"
    abort: bool = False

    def __post_init__(self) -> None:
        if self.capture_method not in ("automatic", "manual"):
            raise ValueError(
                f"capture_method must be 'automatic' or 'manual', got {self.capture_method!r}"
            )
        if not self.name:
            raise ValueError("scenario name must not be empty")
        if self.budget_usd <= 0:
            raise ValueError(f"budget_usd must be positive, got {self.budget_usd}")
        if not self.task:
            raise ValueError("task description must not be empty")
        if self.concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {self.concurrency}")

    @property
    def budget_cents(self) -> int:
        """Convert budget_usd to cents."""
        return usd_to_cents(self.budget_usd)


# ---------------------------------------------------------------------------
# Session & run results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Runtime context for a single harness session.

    Attributes:
        session_id: Unique session identifier (auto-generated).
        seed: Random seed for reproducibility.
        budget_cents: Maximum budget in cents.
        start_time: When the session started.
        stripe_key_prefix: Must be ``sk_test_`` — harness refuses live keys.
    """

    seed: int
    budget_cents: int
    stripe_key_prefix: str
    session_id: str = field(default_factory=_new_id)
    start_time: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        allowed_prefixes = ("sk_test_", "rk_test_")
        if not any(self.stripe_key_prefix.startswith(p) for p in allowed_prefixes):
            raise ValueError(
                f"stripe_key_prefix must start with one of {allowed_prefixes}, "
                f"got {self.stripe_key_prefix!r}. "
                "REFUSING TO RUN with a non-test key."
            )
        if self.budget_cents < 0:
            raise ValueError(f"budget_cents must be non-negative, got {self.budget_cents}")


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    """A detected violation of a payment invariant.

    Attributes:
        invariant_id: Identifier like ``I1``, ``I2``, etc.
        description: Human-readable description of the violation.
        evidence: Supporting data for the violation.
        severity: How severe this violation is.
    """

    invariant_id: str
    description: str
    severity: Severity
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.invariant_id:
            raise ValueError("invariant_id must not be empty")


@dataclass(frozen=True, slots=True)
class RunResult:
    """Complete result of a harness run.

    Attributes:
        session: The session context for this run.
        events: All recorded events during the run.
        violations: Invariant violations detected.
        passed: Whether the run passed all assertions.
        duration_seconds: Wall-clock duration.
    """

    session: SessionContext
    passed: bool
    duration_seconds: float
    events: list[RequestRecord] = field(default_factory=list)
    violations: list[InvariantViolation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """A recorded HTTP request/response pair.

    Attributes:
        record_id: Unique identifier (auto-generated).
        method: HTTP method (GET, POST, etc.).
        path: Request path.
        body: Request body (parsed JSON).
        headers: Request headers.
        idempotency_key: Idempotency-Key header value, if present.
        timestamp: When the request was made.
        response_status: HTTP status code of the response.
        response_body: Response body (parsed JSON).
        response_time_ms: Response time in milliseconds.
        fault_injected: Description of any fault injected on this request.
    """

    method: str
    path: str
    record_id: str = field(default_factory=_new_id)
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    timestamp: datetime = field(default_factory=_utcnow)
    response_status: int | None = None
    response_body: dict[str, Any] | None = None
    response_time_ms: float | None = None
    fault_injected: str | None = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def cents_to_usd(cents: int) -> Decimal:
    """Convert an amount in cents to a USD ``Decimal``.

    >>> cents_to_usd(1050)
    Decimal('10.50')
    """
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd_to_cents(usd: Decimal) -> int:
    """Convert a USD ``Decimal`` to cents (integer).

    >>> usd_to_cents(Decimal("10.50"))
    1050
    """
    return int((usd * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
