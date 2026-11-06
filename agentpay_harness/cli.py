"""Command-line interface for agentpay-harness.

Provides CLI commands to run scenarios, replay traces, validate scenario files,
and list all supported invariants.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agentpay_harness.adapters.protocol import AgentUnderTest
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.report import format_report, load_trace, save_trace
from agentpay_harness.scenario import load_scenario
from agentpay_harness.shrink import shrink


def _load_agent_class(import_path: str | None) -> type[AgentUnderTest]:
    """Dynamically import an AgentUnderTest class by dotted string."""
    if not import_path:
        return ReferenceAgent

    module_name, _, class_name = import_path.rpartition(".")
    if not module_name:
        raise click.BadParameter(
            f"Invalid import path '{import_path}'. Expected format 'pkg.module.ClassName'."
        )

    try:
        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
        return cls  # type: ignore[no-any-return]
    except Exception as exc:
        raise click.ClickException(f"Failed to load agent '{import_path}': {exc}") from exc


@click.group()
@click.version_option(package_name="agentpay-harness")
def main() -> None:
    """agentpay-harness: Fault-injection test harness for AI agent Stripe payments."""


@main.command("run")
@click.argument("scenario_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--agent",
    "agent_path",
    default=None,
    help="Dotted Python path to AgentUnderTest class (defaults to ReferenceAgent).",
)
@click.option(
    "--stripe-key",
    envvar="STRIPE_TEST_SECRET_KEY",
    default=None,
    help="Stripe test API key (sk_test_... or rk_test_...).",
)
@click.option(
    "--base-url",
    default=None,
    help="Optional base URL for stripe-mock server.",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Save execution trace to a JSON file for replay.",
)
@click.option(
    "--shrink-on-failure",
    is_flag=True,
    default=False,
    help="Attempt to minimize fault schedule if run fails.",
)
def run_command(
    scenario_path: Path,
    agent_path: str | None,
    stripe_key: str | None,
    base_url: str | None,
    output: Path | None,
    shrink_on_failure: bool,
) -> None:
    """Run a scenario file, or every *.yaml / *.yml scenario in a directory."""
    console = Console()

    if scenario_path.is_dir():
        scenario_files = sorted(
            p for p in scenario_path.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file()
        )
        if not scenario_files:
            console.print(f"[bold red]No .yaml/.yml scenarios found in {scenario_path}[/bold red]")
            sys.exit(1)
        if output is not None:
            output.mkdir(parents=True, exist_ok=True)
    else:
        scenario_files = [scenario_path]

    agent_cls = _load_agent_class(agent_path)
    outcomes: list[tuple[str, bool]] = []
    for path in scenario_files:
        trace_path = output
        if output is not None and scenario_path.is_dir():
            trace_path = output / f"{path.stem}.json"
        passed = _run_one(
            console, path, agent_cls, stripe_key, base_url, trace_path, shrink_on_failure
        )
        outcomes.append((path.name, passed))

    if len(outcomes) > 1:
        summary = Table(title="[bold]Scenario summary[/bold]", show_header=True)
        summary.add_column("Scenario", style="bold white")
        summary.add_column("Result")
        for name, passed in outcomes:
            summary.add_row(name, "[green]PASSED[/green]" if passed else "[red]FAILED[/red]")
        console.print(summary)

    sys.exit(0 if all(passed for _, passed in outcomes) else 1)


def _run_one(
    console: Console,
    scenario_path: Path,
    agent_cls: type[AgentUnderTest],
    stripe_key: str | None,
    base_url: str | None,
    output: Path | None,
    shrink_on_failure: bool,
) -> bool:
    """Run a single scenario file and return whether it passed."""
    try:
        scenario = load_scenario(scenario_path)
    except Exception as exc:
        console.print(f"[bold red]Failed to load scenario {scenario_path}:[/bold red] {exc}")
        return False

    agent = agent_cls()
    force_offline = base_url is None and (stripe_key is None or "mock" in str(stripe_key))
    driver = HarnessDriver(
        agent=agent,
        scenario=scenario,
        stripe_key=stripe_key,
        stripe_base_url=base_url,
        force_offline=force_offline,
        agent_factory=agent_cls,
    )

    result = driver.run()
    console.print(format_report(result))

    if output:
        save_trace(result, output)
        console.print(f"[dim]Trace saved to {output}[/dim]")

    if not result.passed and shrink_on_failure:
        console.print("[yellow]Shrinking fault schedule to minimal reproduction...[/yellow]")
        shrunk = shrink(
            scenario=scenario,
            agent_factory=agent_cls,
            run_fn=lambda sc, ag: (
                HarnessDriver(
                    agent=ag,
                    scenario=sc,
                    stripe_key=stripe_key,
                    stripe_base_url=base_url,
                    force_offline=force_offline,
                )
                .run()
                .violations
            ),
        )
        console.print(
            f"[bold green]Minimal repro shrunk from {shrunk.original_fault_count} "
            f"to {shrunk.minimal_fault_count} faults in {shrunk.steps} steps.[/bold green]"
        )

    return result.passed


@main.command("replay")
@click.argument("trace_path", type=click.Path(exists=True, path_type=Path))
def replay_command(trace_path: Path) -> None:
    """Replay and inspect a saved execution trace JSON file."""
    console = Console()
    try:
        result = load_trace(trace_path)
    except Exception as exc:
        console.print(f"[bold red]Failed to load trace:[/bold red] {exc}")
        sys.exit(1)

    console.print(format_report(result))
    sys.exit(0 if result.passed else 1)


@main.command("validate")
@click.argument("scenario_path", type=click.Path(exists=True, path_type=Path))
def validate_command(scenario_path: Path) -> None:
    """Validate a scenario definition file without executing it."""
    console = Console()
    try:
        scenario = load_scenario(scenario_path)
        console.print(f"[bold green]✓ Scenario '{scenario.name}' is valid.[/bold green]")
        console.print(f"  Task: {scenario.task}")
        console.print(f"  Budget: ${scenario.budget_usd}")
        console.print(f"  Seed: {scenario.seed}")
        console.print(f"  Faults: {len(scenario.faults)}")
        console.print(f"  Assertions: {scenario.assertions or 'ALL'}")
    except Exception as exc:
        console.print(f"[bold red]Validation error:[/bold red] {exc}")
        sys.exit(1)


@main.group("invariants")
def invariants_group() -> None:
    """Inspect the built-in invariants."""


@invariants_group.command("list")
def invariants_list_command() -> None:
    """List all supported invariants (I1-I13) and what violates them."""
    _print_invariants()


@main.command("list-invariants")
def list_invariants_command() -> None:
    """List all supported invariants (alias for `invariants list`)."""
    _print_invariants()


def _print_invariants() -> None:
    console = Console()
    table = Table(title="[bold]agentpay-harness Invariants (I1-I13)[/bold]", show_header=True)
    table.add_column("ID", style="bold cyan", width=6)
    table.add_column("Invariant", style="bold white", width=36)
    table.add_column("Violated When", style="yellow")

    explanations = [
        (
            "I1",
            "At most one successful charge per intent",
            "Retry without a stable idempotency key",
        ),
        ("I2", "Total captured <= authorized budget", "Concurrent check-then-act race on budget"),
        ("I3", "Every charge maps to approved intent", "Agent loses its own ledger write"),
        (
            "I4",
            "Reused key + same params returns original",
            "Key reuse bug or timestamp-derived key",
        ),
        (
            "I5",
            "Reused key + different params is an error",
            "Agent mutates amount/currency on retry",
        ),
        ("I6", "Each webhook processed at most once", "Missing deduplication table"),
        (
            "I7",
            "Event processing order-independent",
            "Agent trusts arrival order for terminal states",
        ),
        (
            "I8",
            "Webhook signatures verified & in tolerance",
            "Replay attack or missing signature verify",
        ),
        (
            "I9",
            "On abort, no uncaptured auth / charge remains",
            "Missing compensating action or cancel",
        ),
        ("I10", "429 responses produce exponential backoff", "Naive tight retry loop"),
        (
            "I11",
            "Ledger reconciles exactly with Stripe",
            "Partial-failure amnesia / untracked funds",
        ),
        (
            "I12",
            "Ledger books captured, not authorized",
            "Ignoring amount_received after a partial capture",
        ),
        (
            "I13",
            "Refunds never exceed capture",
            "Refunding the authorization / booking a refused refund",
        ),
    ]

    for inv_id, desc, violated_when in explanations:
        table.add_row(inv_id, desc, violated_when)

    console.print(table)


if __name__ == "__main__":
    main()
