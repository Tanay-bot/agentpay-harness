"""End-to-end paths: CLI, traces, HTTP receiver, every agent x scenario, a fake Stripe CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentpay_harness.adapters import broken
from agentpay_harness.adapters.reference import ReferenceAgent
from agentpay_harness.cli import main
from agentpay_harness.driver import HarnessDriver
from agentpay_harness.report import format_report, load_trace, save_trace
from agentpay_harness.scenario import (
    HAS_YAML,
    _simple_yaml_parse,
    load_scenario,
    parse_scenario_data,
)
from agentpay_harness.stripeio.client import RecordingStripeClient
from agentpay_harness.types import FaultConfig, FaultType, ScenarioConfig, StripeEvent
from agentpay_harness.webhooks.receiver import WebhookReceiver
from agentpay_harness.webhooks.signatures import HARNESS_WEBHOOK_SECRET, generate_test_signature

SCENARIOS = sorted(Path("scenarios").glob("*.yaml"))
BROKEN = [
    getattr(broken, name)
    for name in dir(broken)
    if name.startswith("Broken") and isinstance(getattr(broken, name), type)
]


# --- every agent against every scenario ----------------------------------


@pytest.mark.slow
def test_every_broken_agent_is_caught_by_some_scenario() -> None:
    caught: dict[str, list[str]] = {}
    for agent_cls in BROKEN:
        for path in SCENARIOS:
            result = HarnessDriver(
                agent=agent_cls(), scenario=load_scenario(path), force_offline=True
            ).run()
            if not result.passed:
                caught.setdefault(agent_cls.__name__, []).append(path.stem)
    missed = sorted({c.__name__ for c in BROKEN} - set(caught))
    assert not missed, f"no bundled scenario catches: {missed}"
    assert len(BROKEN) == 11


# --- both YAML parsers agree ---------------------------------------------


@pytest.mark.skipif(not HAS_YAML, reason="needs PyYAML to compare against")
@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_fallback_yaml_parser_matches_pyyaml(path: Path) -> None:
    fallback = parse_scenario_data(_simple_yaml_parse(path.read_text(encoding="utf-8")))
    assert fallback == load_scenario(path)


# --- CLI ----------------------------------------------------------------------


def test_cli_run_directory_writes_traces_and_replays(tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "traces"
    result = runner.invoke(main, ["run", "scenarios/", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "Scenario summary" in result.output
    traces = sorted(p.name for p in out.glob("*.json"))
    assert len(traces) == len(SCENARIOS)

    replay = runner.invoke(main, ["replay", str(out / "budget-race.json")])
    assert replay.exit_code == 0, replay.output


def test_cli_run_failing_agent_shrinks_and_exits_1() -> None:
    result = CliRunner().invoke(
        main,
        [
            "run",
            "scenarios/duplicate-on-timeout.yaml",
            "--agent",
            "agentpay_harness.adapters.broken.BrokenIdempotencyAgent",
            "--shrink-on-failure",
        ],
    )
    assert result.exit_code == 1
    assert "Minimal repro shrunk from 4 to 1 faults" in result.output


@pytest.mark.parametrize("args", [["invariants", "list"], ["list-invariants"]])
def test_cli_lists_all_thirteen_invariants(args: list[str]) -> None:
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0
    assert "I13" in result.output


def test_cli_reports_bad_agent_path() -> None:
    result = CliRunner().invoke(
        main, ["run", "scenarios/clock-skew.yaml", "--agent", "nope.Missing"]
    )
    assert result.exit_code != 0
    assert "Failed to load agent" in result.output


def test_cli_validate_prints_summary() -> None:
    result = CliRunner().invoke(main, ["validate", "scenarios/budget-race.yaml"])
    assert result.exit_code == 0
    assert "is valid" in result.output


# --- traces & report --------------------------------------------------------


def test_trace_round_trip_keeps_violations(tmp_path: Path) -> None:
    result = HarnessDriver(
        agent=broken.BrokenRefundAgent(),
        scenario=load_scenario("scenarios/refund-overflow.yaml"),
        force_offline=True,
    ).run()
    assert not result.passed
    path = tmp_path / "trace.json"
    save_trace(result, path)
    restored = load_trace(path)
    assert restored.passed is False
    assert [v.invariant_id for v in restored.violations] == [
        v.invariant_id for v in result.violations
    ]
    assert len(restored.events) == len(result.events)
    assert format_report(restored) is not None


# --- HTTP webhook receiver ---------------------------------------------------


def _post(url: str, body: bytes, header: str) -> int:
    req = urllib.request.Request(  # noqa: S310 - local test server
        url, data=body, headers={"Stripe-Signature": header, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        with exc:
            return int(exc.code)


def test_http_receiver_verifies_signatures() -> None:
    received: list[StripeEvent] = []
    receiver = WebhookReceiver(event_handler=received.append)
    receiver.start()
    try:
        body = json.dumps(
            {"id": "evt_http", "type": "payment_intent.succeeded", "data": {}}
        ).encode()
        good = generate_test_signature(body, HARNESS_WEBHOOK_SECRET)
        bad = generate_test_signature(body, "whsec_wrong")
        assert _post(receiver.url, body, good) == 200
        assert _post(receiver.url, body, bad) == 400
    finally:
        receiver.stop()
    assert [e.event_id for e in received] == ["evt_http"]


def test_http_receiver_raw_mode_passes_bytes_through() -> None:
    raw: list[tuple[bytes, str]] = []
    receiver = WebhookReceiver(
        event_handler=lambda e: None, raw_handler=lambda b, h: raw.append((b, h))
    )
    receiver.start()
    try:
        assert _post(receiver.url, b'{"anything": 1}', "t=1,v1=abc") == 200
    finally:
        receiver.stop()
    assert raw == [(b'{"anything": 1}', "t=1,v1=abc")]


# --- request faults & mock endpoints -----------------------------------------


def test_reference_survives_server_errors_delays_and_duplicate_requests() -> None:
    scenario = ScenarioConfig(
        name="transport-noise",
        budget_usd=Decimal("100"),
        task="buy two API credit packs at $20 each",
        seed=9,
        faults=[
            FaultConfig(type=FaultType.SERVER_ERROR, on="POST /v1/payment_intents", occurrence=1),
            FaultConfig(type=FaultType.DELAY_RESPONSE, on="POST /v1/payment_intents", ms=5),
            FaultConfig(
                type=FaultType.DUPLICATE_REQUEST, on="POST /v1/payment_intents", occurrence=3
            ),
        ],
        assertions=["I1", "I2", "I4", "I11"],
    )
    result = HarnessDriver(agent=ReferenceAgent(), scenario=scenario, force_offline=True).run()
    assert result.passed, result.violations
    assert any(r.response_status and r.response_status >= 500 for r in result.events)


def test_offline_mock_endpoints() -> None:
    client = RecordingStripeClient(api_key="sk_test_mock_endpoints", force_offline=True)
    pi = client.create_payment_intent(1000, "usd", idempotency_key="k1", capture_method="manual")
    assert pi["status"] == "requires_capture"
    assert client.create_refund(pi["id"])["_status_code"] == 400  # nothing captured yet
    assert client.capture_payment_intent(pi["id"])["status"] == "succeeded"
    assert client.capture_payment_intent(pi["id"])["_status_code"] == 400  # already captured
    assert client.create_refund(pi["id"], amount=400)["amount"] == 400
    assert client.create_refund(pi["id"], amount=700)["_status_code"] == 400  # only 600 left
    assert client.retrieve_payment_intent("pi_missing")["_status_code"] == 404
    assert client.cancel_payment_intent("pi_missing")["_status_code"] == 404
    assert [p["id"] for p in client.list_payment_intents()] == [pi["id"]]
    assert len(client.list_balance_transactions()) == 1


# --- fake Stripe CLI: the stripe_cli mode without network --------------------

_FAKE_STRIPE = """\
#!{python}
import hashlib, hmac, json, os, sys, time, urllib.request
SECRET = "whsec_fake_cli_secret"
STATE = os.environ["FAKE_STRIPE_STATE"]
args = sys.argv[1:]
if args[:2] == ["listen", "--print-secret"]:
    print(SECRET)
elif args[:2] == ["listen", "--forward-to"]:
    open(STATE, "w").write(args[2])
    print("> Ready! You are using Stripe API Version [fake]", file=sys.stderr, flush=True)
    while True:
        time.sleep(0.2)
elif args[0] == "trigger":
    url = open(STATE).read()
    body = json.dumps({{
        "id": "evt_cli_" + args[1].replace(".", "_"), "object": "event", "type": args[1],
        "created": int(time.time()), "livemode": False,
        "data": {{"object": {{"id": "pi_cli_1", "object": "payment_intent", "amount": 2000,
                              "status": "succeeded", "metadata": {{}}}}}},
    }}).encode()
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{{ts}}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {{"Stripe-Signature": f"t={{ts}},v1={{sig}}"}}
    req = urllib.request.Request(url, data=body, headers=headers)
    urllib.request.urlopen(req, timeout=5).read()
else:
    sys.exit(2)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX script")
def test_stripe_cli_mode_with_a_fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "stripe"
    script.write_text(_FAKE_STRIPE.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STRIPE_STATE", str(tmp_path / "state"))

    agent = ReferenceAgent()
    driver = HarnessDriver(
        agent=agent,
        scenario=load_scenario("scenarios/integration/stripe-cli-webhooks.yaml"),
        stripe_key="sk_test_fake_key_for_cli",
        force_offline=True,
    )
    result = driver.run()
    assert result.passed, result.violations
    # The genuine event verified with the CLI's secret; the forgery did not.
    assert "evt_cli_payment_intent_succeeded" in agent._seen_event_ids
    assert not any(e.startswith("evt_forged") for e in agent._seen_event_ids)
    assert agent._webhook_secret == "whsec_fake_cli_secret"


def test_fake_cli_script_is_valid_python() -> None:
    compile(textwrap.dedent(_FAKE_STRIPE.format(python=sys.executable)), "<fake>", "exec")
