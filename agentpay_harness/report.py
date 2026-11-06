"""Report generation and trace serialization for agentpay-harness.

Formats rich terminal reports summarizing scenario runs, invariant checks,
event timelines, and minimal reproduction cases. Supports JSON trace export/import
for deterministically reproducible bug reports.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import (
    InvariantViolation,
    RequestRecord,
    RunResult,
    SessionContext,
    Severity,
    cents_to_usd,
)


def format_report(result: RunResult) -> str:
    """Generate a rich, human-readable terminal report of a scenario execution."""
    string_io = io.StringIO()
    console = Console(file=string_io, force_terminal=True, width=100)

    # Status header
    status_style = "bold green" if result.passed else "bold red"
    status_text = "PASSED" if result.passed else "FAILED"
    console.print(
        Panel(
            f"[{status_style}]agentpay-harness — Run {status_text}[/{status_style}]\n"
            f"Session ID: [cyan]{result.session.session_id}[/cyan] | "
            f"Seed: [bold]{result.session.seed}[/bold] | "
            f"Duration: [bold]{result.duration_seconds:.2f}s[/bold] | "
            f"Budget: [yellow]${cents_to_usd(result.session.budget_cents)}[/yellow]",
            title="[bold]Summary[/bold]",
            border_style="green" if result.passed else "red",
        )
    )

    # Invariant violations table
    if result.violations:
        inv_table = Table(
            title="[bold red]Invariant Violations Detected[/bold red]", show_header=True
        )
        inv_table.add_column("ID", style="bold red", width=6)
        inv_table.add_column("Severity", style="bold yellow", width=10)
        inv_table.add_column("Description", style="white")

        for v in result.violations:
            inv_table.add_row(v.invariant_id, v.severity.value.upper(), v.description)

        console.print(inv_table)
    else:
        console.print("\n[bold green]✓ All asserted payment invariants held true.[/bold green]\n")

    # Request / Event timeline table
    if result.events:
        timeline_table = Table(title="Recorded Stripe Interactions", show_header=True)
        timeline_table.add_column("Method", width=8)
        timeline_table.add_column("Path", width=36)
        timeline_table.add_column("Status", width=8)
        timeline_table.add_column("Idempotency Key", width=22)
        timeline_table.add_column("Fault Injected", style="bold magenta")

        for req in result.events:
            status_color = (
                "green"
                if req.response_status == 200
                else ("yellow" if req.response_status == 429 else "red")
            )
            timeline_table.add_row(
                req.method,
                req.path,
                f"[{status_color}]{req.response_status or 'TIMEOUT'}[/{status_color}]",
                (req.idempotency_key[:18] + "…")
                if req.idempotency_key and len(req.idempotency_key) > 20
                else (req.idempotency_key or "-"),
                req.fault_injected or "-",
            )

        console.print(timeline_table)

    return string_io.getvalue()


def format_timeline(recorder: EventRecorder) -> str:
    """Format the full recorder timeline as a string table."""
    string_io = io.StringIO()
    console = Console(file=string_io, force_terminal=True, width=100)

    table = Table(title="Timeline Entries", show_header=True)
    table.add_column("Time", width=12)
    table.add_column("Type", width=10)
    table.add_column("Details")

    for entry in recorder.get_timeline():
        t_str = entry.timestamp.strftime("%H:%M:%S.%f")[:-3]
        if entry.entry_type == "request" and entry.request:
            req = entry.request
            details = f"{req.method} {req.path} -> {req.response_status}"
            if req.fault_injected:
                details += f" [fault: {req.fault_injected}]"
        elif entry.entry_type == "event" and entry.event:
            evt = entry.event
            details = f"Webhook: {evt.event_type} (id={evt.event_id})"
        else:
            details = "-"
        table.add_row(t_str, entry.entry_type, details)

    console.print(table)
    return string_io.getvalue()


def save_trace(result: RunResult, path: Path | str) -> None:
    """Serialize a RunResult to a JSON trace file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    trace_data: dict[str, Any] = {
        "session": {
            "session_id": result.session.session_id,
            "seed": result.session.seed,
            "budget_cents": result.session.budget_cents,
            "stripe_key_prefix": result.session.stripe_key_prefix,
            "start_time": result.session.start_time.isoformat(),
        },
        "passed": result.passed,
        "duration_seconds": result.duration_seconds,
        "violations": [
            {
                "invariant_id": v.invariant_id,
                "description": v.description,
                "severity": v.severity.value,
                "evidence": v.evidence,
            }
            for v in result.violations
        ],
        "events": [
            {
                "record_id": req.record_id,
                "method": req.method,
                "path": req.path,
                "body": req.body,
                "headers": req.headers,
                "idempotency_key": req.idempotency_key,
                "timestamp": req.timestamp.isoformat(),
                "response_status": req.response_status,
                "response_body": req.response_body,
                "response_time_ms": req.response_time_ms,
                "fault_injected": req.fault_injected,
            }
            for req in result.events
        ],
    }

    p.write_text(json.dumps(trace_data, indent=2), encoding="utf-8")


def load_trace(path: Path | str) -> RunResult:
    """Deserialize a saved JSON trace file back into a RunResult."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))

    s_data = data["session"]
    session = SessionContext(
        session_id=s_data["session_id"],
        seed=s_data["seed"],
        budget_cents=s_data["budget_cents"],
        stripe_key_prefix=s_data["stripe_key_prefix"],
        start_time=datetime.fromisoformat(s_data["start_time"]),
    )

    violations = [
        InvariantViolation(
            invariant_id=v["invariant_id"],
            description=v["description"],
            severity=Severity(v["severity"]),
            evidence=v.get("evidence", {}),
        )
        for v in data.get("violations", [])
    ]

    events = [
        RequestRecord(
            record_id=req["record_id"],
            method=req["method"],
            path=req["path"],
            body=req.get("body"),
            headers=req.get("headers", {}),
            idempotency_key=req.get("idempotency_key"),
            timestamp=datetime.fromisoformat(req["timestamp"]),
            response_status=req.get("response_status"),
            response_body=req.get("response_body"),
            response_time_ms=req.get("response_time_ms"),
            fault_injected=req.get("fault_injected"),
        )
        for req in data.get("events", [])
    ]

    return RunResult(
        session=session,
        passed=data["passed"],
        duration_seconds=data["duration_seconds"],
        violations=violations,
        events=events,
    )
