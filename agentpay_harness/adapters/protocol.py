"""Protocol definitions for agents and Stripe clients.

These are structural (duck-typed) protocols — any class that implements the
required methods is compatible without explicit inheritance.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agentpay_harness.types import (
    Intent,
    LedgerEntry,
    PaymentResult,
    StripeEvent,
    Task,
)


@runtime_checkable
class StripeClient(Protocol):
    """Protocol for a Stripe API client.

    The harness provides a fault-injecting implementation; agents code against
    this protocol so they remain decoupled from the real Stripe SDK.
    """

    def create_payment_intent(
        self,
        amount: int,
        currency: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new PaymentIntent.

        Args:
            amount: Amount in smallest currency unit (e.g. cents).
            currency: ISO 4217 currency code.
            idempotency_key: Optional idempotency key for safe retries.
            metadata: Optional metadata to attach to the PaymentIntent.

        Returns:
            The created PaymentIntent object as a dict.
        """
        ...

    def capture_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Capture a previously authorized PaymentIntent.

        Args:
            payment_intent_id: The ``pi_...`` identifier to capture.
            idempotency_key: Optional idempotency key for safe retries.

        Returns:
            The captured PaymentIntent object as a dict.
        """
        ...

    def cancel_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        """Cancel an uncaptured PaymentIntent.

        Args:
            payment_intent_id: The ``pi_...`` identifier to cancel.

        Returns:
            The canceled PaymentIntent object as a dict.
        """
        ...

    def retrieve_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        """Retrieve the current state of a PaymentIntent.

        Args:
            payment_intent_id: The ``pi_...`` identifier to retrieve.

        Returns:
            The PaymentIntent object as a dict.
        """
        ...

    def list_payment_intents(
        self,
        *,
        limit: int = 100,
        created: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """List PaymentIntents, optionally filtered by creation time.

        Args:
            limit: Maximum number of results (1-100).
            created: Optional time-range filter dict (``gte``, ``lte``, etc.).

        Returns:
            A list of PaymentIntent objects.
        """
        ...

    def list_balance_transactions(
        self,
        *,
        limit: int = 100,
        created: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """List balance transactions, optionally filtered by creation time.

        Args:
            limit: Maximum number of results (1-100).
            created: Optional time-range filter dict.

        Returns:
            A list of BalanceTransaction objects.
        """
        ...


@runtime_checkable
class AgentUnderTest(Protocol):
    """Protocol that every agent must satisfy to be tested by the harness.

    The harness drives the agent through a plan → pay → reconcile lifecycle:

    1. ``plan`` — given a task, return a list of payment intents.
    2. ``pay``  — execute a single intent via the provided Stripe client.
    3. ``on_event`` — handle an incoming webhook event.
    4. ``ledger`` — return the agent's current view of all payments.
    """

    def plan(self, task: Task) -> list[Intent]:
        """Decompose a task into payment intents.

        Args:
            task: The high-level task (including budget).

        Returns:
            Ordered list of intents the agent plans to execute.
        """
        ...

    def pay(self, intent: Intent, client: StripeClient) -> PaymentResult:
        """Execute a single payment intent.

        The agent MUST use the provided ``client`` (not its own) so the harness
        can intercept and inject faults.

        Args:
            intent: The intent to execute.
            client: A (possibly fault-injecting) Stripe client.

        Returns:
            The result of the payment attempt.
        """
        ...

    def on_event(self, event: StripeEvent) -> None:
        """Handle an incoming Stripe webhook event.

        The agent should update its internal state (ledger, etc.) as needed.
        Duplicate or out-of-order events must be handled gracefully.

        Args:
            event: The webhook event to process.
        """
        ...

    def ledger(self) -> list[LedgerEntry]:
        """Return the agent's current payment ledger.

        Returns:
            A snapshot of all ledger entries, in creation order.
        """
        ...
