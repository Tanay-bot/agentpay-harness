"""Scenario validation: every malformed scenario is rejected with a clear, field-named error."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from agentpay_harness.cli import main
from agentpay_harness.invariants.checker import DEFAULT_REGISTRY
from agentpay_harness.scenario import (
    HAS_YAML,
    VALID_INVARIANTS,
    ScenarioValidationError,
    load_scenario,
    parse_scenario_data,
)
from agentpay_harness.types import FaultType


def _base(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "t",
        "budget_usd": 100,
        "task": "buy one pack at $50",
        "seed": 1,
    }
    raw.update(overrides)
    return raw


def _rejects(raw: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(ScenarioValidationError) as exc:
        parse_scenario_data(raw)
    for fragment in fragments:
        assert fragment in str(exc.value), str(exc.value)


def test_valid_minimal_scenario_parses() -> None:
    sc = parse_scenario_data(_base())
    assert sc.budget_cents == 10000


@pytest.mark.parametrize("path", sorted(Path("scenarios").glob("*.yaml")), ids=lambda p: p.stem)
def test_every_bundled_scenario_validates(path: Path) -> None:
    load_scenario(path)


def test_invariant_ids_match_registry() -> None:
    assert set(DEFAULT_REGISTRY._checkers) == VALID_INVARIANTS


# --- unknown fault types --------------------------------------------------


def test_rejects_unknown_fault_type() -> None:
    _rejects(_base(faults=[{"type": "explode"}]), "faults[0].type", "unknown fault type 'explode'")


def test_rejects_fault_without_type() -> None:
    _rejects(_base(faults=[{"on": "POST /v1/payment_intents"}]), "faults[0].type", "missing")


def test_rejects_unknown_fault_field() -> None:
    _rejects(
        _base(faults=[{"type": "rate_limit", "on": "POST /v1/x", "ocurrence": 1}]),
        "faults[0]",
        "ocurrence",
    )


def test_rejects_request_fault_without_on() -> None:
    _rejects(_base(faults=[{"type": "rate_limit"}]), "faults[0].on", "needs 'on")


def test_rejects_malformed_on_pattern() -> None:
    _rejects(_base(faults=[{"type": "rate_limit", "on": "payment_intents"}]), "faults[0].on")


def test_rejects_webhook_fault_without_event() -> None:
    _rejects(_base(faults=[{"type": "duplicate_webhook", "times": 2}]), "faults[0].event")


def test_rejects_non_integer_occurrence() -> None:
    _rejects(
        _base(faults=[{"type": "rate_limit", "on": "POST /v1/x", "occurrence": "two"}]),
        "faults[0].occurrence",
        "integer",
    )


def test_rejects_partial_capture_without_percent() -> None:
    _rejects(_base(faults=[{"type": "partial_capture"}]), "faults[0].percent")


def test_rejects_zero_clock_skew() -> None:
    _rejects(_base(faults=[{"type": "clock_skew", "skew_s": 0}]), "faults[0].skew_s")


def test_rejects_interleave_without_multiple_agents() -> None:
    _rejects(_base(faults=[{"type": "interleave"}]), "agents: 2")


def test_rejects_interleave_schedule_with_bad_agent_index() -> None:
    _rejects(
        _base(agents=2, faults=[{"type": "interleave", "schedule": [0, 2]}]),
        "faults[0].schedule",
    )


# --- unknown invariant ids ------------------------------------------------


def test_rejects_unknown_invariant() -> None:
    _rejects(_base(**{"assert": ["I1", "I99"]}), "assert[1]", "unknown invariant 'I99'")


def test_rejects_assert_that_is_not_a_list() -> None:
    _rejects(_base(**{"assert": "I1"}), "assert", "must be a list")


# --- malformed budgets ----------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "fragment"),
    [
        (None, "missing"),
        ("abc", "must be a number"),
        (True, "must be a number"),
        (0, "greater than 0"),
        (-5, "greater than 0"),
        ("NaN", "finite"),
        ("Infinity", "finite"),
        ("10.001", "2 decimal places"),
        ([100], "must be a number"),
    ],
)
def test_rejects_malformed_budget(budget: Any, fragment: str) -> None:
    raw = _base()
    if budget is None:
        del raw["budget_usd"]
    else:
        raw["budget_usd"] = budget
    _rejects(raw, "budget_usd", fragment)


# --- other top-level fields -----------------------------------------------


def test_rejects_unknown_top_level_field() -> None:
    _rejects(_base(asserts=["I1"]), "unknown field", "asserts")


def test_rejects_missing_seed() -> None:
    raw = _base()
    del raw["seed"]
    _rejects(raw, "seed", "missing")


def test_rejects_non_usd_currency() -> None:
    _rejects(_base(currency="eur"), "currency", "USD-only")


def test_rejects_bad_boolean() -> None:
    _rejects(_base(abort="maybe"), "abort", "true or false")


def test_rejects_stripe_cli_without_triggers() -> None:
    _rejects(_base(webhook_source="stripe_cli"), "cli_triggers")


def test_partial_capture_defaults_to_capture_endpoint() -> None:
    sc = parse_scenario_data(_base(faults=[{"type": "partial_capture", "percent": 50}]))
    assert sc.faults[0].type is FaultType.PARTIAL_CAPTURE
    assert sc.faults[0].on == "POST /v1/payment_intents/*/capture"


# --- CLI ------------------------------------------------------------------


def test_validate_command_reports_error_and_exits_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nbudget_usd: lots\ntask: buy\nseed: 1\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "budget_usd" in result.output


@pytest.mark.skipif(not HAS_YAML, reason="needs PyYAML (installed with the package)")
def test_validate_command_rejects_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "not valid YAML" in result.output
