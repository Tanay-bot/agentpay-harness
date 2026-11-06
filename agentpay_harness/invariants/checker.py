"""Invariant checking framework.

Provides the ``InvariantChecker`` protocol, the ``InvariantRegistry`` for
managing and executing checks, and the ``DEFAULT_REGISTRY`` singleton with
all eleven built-in invariants (I1-I11) pre-registered.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from agentpay_harness.types import (
    InvariantViolation,
    LedgerEntry,
    SessionContext,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InvariantChecker(Protocol):
    """Protocol every invariant check must satisfy.

    Attributes:
        invariant_id: Short identifier such as ``"I1"``.
        description: Human-readable description of the property being checked.
    """

    invariant_id: str
    description: str

    def check(
        self,
        recorder: object,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        """Run this invariant check and return any violations.

        Args:
            recorder: An ``EventRecorder`` containing the timeline of
                Stripe requests, responses, and webhook deliveries.
            ledger: The agent's reported ledger entries.
            session: The session context (budget, seed, etc.).

        Returns:
            A (possibly empty) list of violations detected.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class InvariantRegistry:
    """Registry of invariant checkers.

    Supports registration, lookup by ID, and batch execution.
    """

    def __init__(self) -> None:
        self._checkers: dict[str, InvariantChecker] = {}

    # -- mutators -----------------------------------------------------------

    def register(self, checker: InvariantChecker) -> None:
        """Register a checker.

        Args:
            checker: An object satisfying the ``InvariantChecker`` protocol.

        Raises:
            ValueError: If a checker with the same ``invariant_id`` is
                already registered.
        """
        if checker.invariant_id in self._checkers:
            raise ValueError(f"Invariant {checker.invariant_id!r} is already registered")
        self._checkers[checker.invariant_id] = checker
        logger.debug("Registered invariant %s: %s", checker.invariant_id, checker.description)

    # -- queries ------------------------------------------------------------

    def get(self, invariant_id: str) -> InvariantChecker:
        """Return the checker registered under *invariant_id*.

        Raises:
            KeyError: If no checker with that ID exists.
        """
        try:
            return self._checkers[invariant_id]
        except KeyError:
            raise KeyError(
                f"Unknown invariant {invariant_id!r}. Registered: {sorted(self._checkers)}"
            ) from None

    def all_ids(self) -> list[str]:
        """Return all registered invariant IDs in sorted order."""
        return sorted(self._checkers)

    def all_checkers(self) -> list[InvariantChecker]:
        """Return all registered checkers in ID-sorted order."""
        return [self._checkers[k] for k in sorted(self._checkers)]

    # -- execution ----------------------------------------------------------

    def check_all(
        self,
        ids: list[str],
        recorder: object,
        ledger: list[LedgerEntry],
        session: SessionContext,
    ) -> list[InvariantViolation]:
        """Run the specified invariant checks and aggregate violations.

        Args:
            ids: Invariant IDs to execute.  An empty list means *all*.
            recorder: The ``EventRecorder`` for the current session.
            ledger: The agent's reported ledger entries.
            session: The current session context.

        Returns:
            Aggregated list of violations across all requested checks.
        """
        target_ids = ids if ids else sorted(self._checkers)
        violations: list[InvariantViolation] = []
        for inv_id in target_ids:
            checker = self.get(inv_id)
            logger.info("Running invariant %s: %s", inv_id, checker.description)
            try:
                result = checker.check(recorder, ledger, session)
                violations.extend(result)
                if result:
                    logger.warning("Invariant %s FAILED with %d violation(s)", inv_id, len(result))
                else:
                    logger.info("Invariant %s PASSED", inv_id)
            except Exception:
                logger.exception("Invariant %s raised an unexpected exception", inv_id)
                violations.append(
                    InvariantViolation(
                        invariant_id=inv_id,
                        description=f"Invariant {inv_id} raised an exception during check",
                        severity=_severity_module().Severity.ERROR,
                        evidence={"error": "see logs for traceback"},
                    )
                )
        return violations

    def __len__(self) -> int:
        return len(self._checkers)

    def __contains__(self, invariant_id: str) -> bool:
        return invariant_id in self._checkers

    def __repr__(self) -> str:
        return f"InvariantRegistry(ids={sorted(self._checkers)})"


def _severity_module() -> type:
    """Lazy import to avoid circular dependency at module scope."""
    from agentpay_harness import types as _t

    return _t  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Default registry (populated lazily on first access)
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY: InvariantRegistry | None = None


def _build_default_registry() -> InvariantRegistry:
    """Build and return a registry with all I1-I11 checks."""
    from agentpay_harness.invariants.checks import (
        AbortCleansUp,
        AtMostOneChargePerIntent,
        EventProcessingOrderIndependent,
        EveryChargeHasApprovedIntent,
        IdempotencyKeyMismatchIsError,
        IdempotencyKeyReturnsOriginal,
        LedgerReconcilesWithStripe,
        RateLimitBackoff,
        TotalCapturedWithinBudget,
        WebhookEventProcessedAtMostOnce,
        WebhookSignaturesVerified,
    )

    registry = InvariantRegistry()
    for cls in (
        AtMostOneChargePerIntent,
        TotalCapturedWithinBudget,
        EveryChargeHasApprovedIntent,
        IdempotencyKeyReturnsOriginal,
        IdempotencyKeyMismatchIsError,
        WebhookEventProcessedAtMostOnce,
        EventProcessingOrderIndependent,
        WebhookSignaturesVerified,
        AbortCleansUp,
        RateLimitBackoff,
        LedgerReconcilesWithStripe,
    ):
        registry.register(cls())
    return registry


def get_default_registry() -> InvariantRegistry:
    """Return the module-level singleton ``DEFAULT_REGISTRY``.

    The registry is created lazily on first call so that importing the module
    does not trigger heavy work.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = _build_default_registry()
    return _DEFAULT_REGISTRY


#: Module-level convenience alias.  Use ``get_default_registry()`` when you
#: need to guarantee the singleton is initialised.
class _RegistryProxy:
    """Transparent proxy that defers construction until first attribute access."""

    def __getattr__(self, name: str) -> object:
        return getattr(get_default_registry(), name)

    def __repr__(self) -> str:
        return repr(get_default_registry())

    def __len__(self) -> int:
        return len(get_default_registry())

    def __contains__(self, item: object) -> bool:
        return item in get_default_registry()


DEFAULT_REGISTRY: InvariantRegistry = _RegistryProxy()  # type: ignore[assignment]
