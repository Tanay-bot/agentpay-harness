"""Fault schedule shrinking algorithm.

When a scenario run detects invariant violations, shrink() minimizes the fault
schedule to find the minimal subset of faults that still triggers the violation.
This produces the minimal reproduction case for debugging.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.types import InvariantViolation, ScenarioConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShrinkResult:
    """Result of shrinking a failing fault schedule.

    Attributes:
        original_fault_count: Number of faults in the initial scenario.
        minimal_fault_count: Number of faults in the minimized scenario.
        minimal_scenario: The minimized scenario config.
        steps: Total test execution iterations performed during shrinking.
    """

    original_fault_count: int
    minimal_fault_count: int
    minimal_scenario: ScenarioConfig
    steps: int


def shrink(
    scenario: ScenarioConfig,
    agent_factory: Callable[[], AgentUnderTest],
    run_fn: Callable[[ScenarioConfig, AgentUnderTest], list[InvariantViolation]],
    target_invariant_id: str | None = None,
) -> ShrinkResult:
    """Minimize a failing fault schedule using 1-minimal delta debugging.

    Iteratively removes faults one by one. If removing a fault still triggers
    the target violation, that fault is permanently discarded.
    """
    current_faults = list(scenario.faults)
    original_count = len(current_faults)

    if original_count <= 1:
        return ShrinkResult(
            original_fault_count=original_count,
            minimal_fault_count=original_count,
            minimal_scenario=scenario,
            steps=0,
        )

    steps = 0
    changed = True

    while changed and len(current_faults) > 1:
        changed = False
        i = 0
        while i < len(current_faults):
            # Candidate: remove fault at index i
            candidate_faults = current_faults[:i] + current_faults[i + 1 :]
            candidate_scenario = replace(
                scenario, name=f"{scenario.name}-shrunk", faults=candidate_faults
            )

            steps += 1
            test_agent = agent_factory()
            violations = run_fn(candidate_scenario, test_agent)

            # Check if violation still reproduced
            reproduced = False
            if target_invariant_id:
                reproduced = any(v.invariant_id == target_invariant_id for v in violations)
            else:
                reproduced = len(violations) > 0

            if reproduced:
                # Successfully removed this fault! Keep it removed
                current_faults = candidate_faults
                changed = True
                logger.info("Shrunk fault schedule to %d faults", len(current_faults))
                # Do not advance i, test new item at index i
            else:
                i += 1

    minimal_scenario = replace(scenario, name=f"{scenario.name}-minimal", faults=current_faults)

    return ShrinkResult(
        original_fault_count=original_count,
        minimal_fault_count=len(current_faults),
        minimal_scenario=minimal_scenario,
        steps=steps,
    )
