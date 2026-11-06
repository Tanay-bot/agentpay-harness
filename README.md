# agentpay-harness

[![CI](https://github.com/Tanay-bot/agentpay-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Tanay-bot/agentpay-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Fault-injection test harness for AI-agent-initiated Stripe payments.**

AI agents that spend real money need to be tested against the same failure modes that break human-written payment code — timeouts, retries, duplicate webhooks, race conditions, and more.  `agentpay-harness` provides a YAML-driven scenario runner that injects those faults deterministically and verifies a set of safety invariants after every run.

---

## What This Tests

The harness targets these failure modes in AI-agent payment flows (details in [docs/FAILURES.md](docs/FAILURES.md)):

| # | Failure Mode | What Goes Wrong | Status |
|---|---|---|---|
| F1 | **Timeout → Duplicate Charge** | A dropped response triggers a retry; agent creates two PaymentIntents for one purchase. | `duplicate-on-timeout.yaml` |
| F2 | **Runaway Spend (Race)** | Concurrent purchases (or two agent instances) each pass a budget check, but together exceed the limit. | `runaway-spend.yaml`, `budget-race.yaml` |
| F3 | **Webhook Duplication** | Stripe retries a webhook; agent processes `payment_intent.succeeded` twice. | `webhook-chaos.yaml` |
| F4 | **Webhook Reordering** | Events arrive out of order; an agent that drops "older" events misses an async payment failure. | `webhook-out-of-order.yaml` |
| F5 | **Stale Webhook Replay / Clock Skew** | An event arrives with a signature timestamp outside the tolerance window. | `forged-webhook.yaml`, `clock-skew.yaml` |
| F6 | **Partial Capture Mismatch** | Stripe captures less than was authorized; agent books the authorization. | `partial-capture.yaml` |
| F7 | **Refund Exceeds Capture** | Agent refunds (or books a refund of) more than was captured. | `refund-overflow.yaml` |
| F8 | **Forged Webhook** | An event signed with the wrong secret claims a payment succeeded. | `forged-webhook.yaml` |
| F9 | **Abandoned Authorization** | Task is aborted after authorizing; holds are left on the card. | `abort-cleanup.yaml` |
| F10 | **Rate-Limit Hammering** | Agent retries 429s immediately instead of backing off. | `rate-limit-storm.yaml` |

---

## Quickstart

```bash
# Install (Python 3.10+). From source until the first PyPI release:
pip install -e ".[dev]"

# Optional: use a Stripe test-mode key. With no key set, runs are fully offline.
export STRIPE_TEST_SECRET_KEY=sk_test_...

# Run one scenario
agentpay run scenarios/duplicate-on-timeout.yaml

# Run every scenario in a directory (prints a summary, exits 1 if any fail)
agentpay run scenarios/

# Run against one of the deliberately broken agents to watch the harness catch it
agentpay run scenarios/runaway-spend.yaml --agent agentpay_harness.adapters.broken.BrokenBudgetAgent

# Save traces (a directory run writes one <scenario>.json per scenario)
agentpay run scenarios/ -o traces/
agentpay replay traces/runaway-spend.json

# Shrink a failing fault schedule to a minimal reproduction
agentpay run scenarios/duplicate-on-timeout.yaml --agent agentpay_harness.adapters.broken.BrokenIdempotencyAgent --shrink-on-failure

# Validate a scenario file without running it
agentpay validate scenarios/webhook-chaos.yaml

# List available invariants
agentpay invariants list

# Genuine Stripe test-mode webhooks via the Stripe CLI (needs `stripe` on PATH + a test key)
STRIPE_TEST_SECRET_KEY=sk_test_... agentpay run scenarios/integration/stripe-cli-webhooks.yaml
```

To test your own agent, implement the `AgentUnderTest` protocol (`plan`, `pay`, `on_event`, `ledger`) from `agentpay_harness/adapters/protocol.py` and pass its dotted path to `--agent`.  Scenarios that abort, capture or refund also call `abort(client)`, `capture_authorized(client)` and `refund_captured(client)` if your agent has them.

---

## Architecture

```
agentpay_harness/
├── cli.py                 # Click CLI: run, replay, validate, invariants list
├── driver.py              # HarnessDriver — runs a scenario against one or more agent instances
├── scenario.py            # YAML scenario loading & validation
├── types.py               # Shared dataclasses / enums (Task, Intent, FaultConfig, ...)
├── safety.py              # Live-key refusal, livemode response guard
├── ledger.py              # Ledger ↔ Stripe reconciliation
├── report.py              # Rich report output, trace save/load
├── shrink.py              # Fault-schedule shrinker (minimal repro)
├── budget.py              # SharedBudget + seeded InterleavingScheduler (multi-agent races)
├── stripecli.py           # Drives `stripe listen` / `stripe trigger` for genuine webhooks
│
├── adapters/
│   ├── protocol.py        # AgentUnderTest / StripeClient protocols
│   ├── reference.py       # ReferenceAgent — correct baseline that passes every scenario
│   └── broken.py          # 11 deliberately broken agents, one per bug class
│
├── faults/
│   ├── engine.py          # Seeded fault engine: matches rules to requests & webhooks
│   ├── transport.py       # Request faults: drop, 429, 5xx, delay, duplicate
│   ├── webhook.py         # Webhook faults: duplicate, reorder, replay
│   └── actions.py         # FaultAction / WebhookAction types
│
├── stripeio/
│   ├── client.py          # RecordingStripeClient (real test mode, stripe-mock, or offline mock)
│   └── recorder.py        # EventRecorder — timeline of requests & webhooks
│
├── webhooks/
│   ├── receiver.py        # Webhook delivery system + local HTTP receiver
│   └── signatures.py      # Stripe-style HMAC signature signing / verification
│
└── invariants/
    ├── checker.py         # Invariant registry
    └── checks.py          # I1–I13

scenarios/                 # 11 offline scenarios, one or more per failure mode
scenarios/integration/     # Stripe CLI scenario (real test mode; not run by `agentpay run scenarios/`)
.github/workflows/         # CI (lint, mypy, tests + coverage on 3.10–3.12) and PyPI publishing
docs/FAILURES.md           # Failure mode catalog
tests/                     # pytest suite (see "Tests for the Tester")
```

### How a run works

1. Each agent instance **plans** the task into intents, then **pays** each one through a recording Stripe client that applies request faults.  With `agents: 2+`, the instances run in parallel threads against one shared budget; an `interleave` fault fixes the order in which they pass budget checkpoints, so races are reproducible.
2. Webhook events (`payment_intent.created`, `.succeeded`, `.payment_failed`) are **queued** with increasing timestamps, then **delivered** after the payments the way Stripe delivers them, asynchronously, each to the agent that made the payment. Each delivery is signed (`StripeEvent.payload` + `StripeEvent.signature_header`) with `HARNESS_WEBHOOK_SECRET`. Duplicates, reordering, stale replays, clock skew and forgeries are applied at this stage.  With `webhook_source: stripe_cli`, the events are instead genuine Stripe deliveries with Stripe's own signatures.
3. Optional lifecycle steps: `abort: true` → `abort(client)`; `settle: true` → `capture_authorized(client)`; `refund: true` → `refund_captured(client)`.
4. The invariants are checked against the recorded requests, deliveries and the combined ledger of every agent.

### Fault types

| Fault | Applies to | Effect |
|---|---|---|
| `drop_response` | request | Stripe processes the request; the response is lost (timeout) |
| `rate_limit` | request | 429 response |
| `server_error` | request | 5xx response |
| `delay_response` | request | Response delayed by `ms` |
| `duplicate_request` | request | Request sent twice |
| `async_payment_failure` | request | PaymentIntent comes back `processing`, then fails; only a webhook says so (offline mode only) |
| `partial_capture` | request | Capture takes only `percent` of the authorized amount (offline mode only) |
| `duplicate_webhook` | webhook | Event delivered `times` times |
| `reorder_webhooks` | webhook | Deliveries shuffled within `window` (always out of order) |
| `replay_webhook` | webhook | Event re-delivered later with its original signature, `delay_s` old (stale past 300s) |
| `forged_webhook` | webhook | Extra event for a fake PaymentIntent, signed with the wrong secret |
| `clock_skew` | webhook | Signature timestamps shifted by `skew_s` (optionally only for `event`); past ±300 s they are invalid |
| `interleave` | budget | Agent instances take turns at budget checkpoints in `schedule` order (or a seeded order) |

### Scenario options

| Field | Default | Meaning |
|---|---|---|
| `name`, `task`, `budget_usd`, `seed` | required | Budget must be a positive number with at most 2 decimals |
| `assert` | all | Invariant ids to check |
| `faults` | none | See the table above |
| `concurrency` | 1 | Pay one agent's intents in parallel threads |
| `agents` | 1 | Agent instances sharing one budget |
| `capture_method` | `automatic` | `manual` = authorize now, capture later |
| `abort` / `settle` / `refund` | `false` | Call `abort` / `capture_authorized` / `refund_captured` after paying |
| `currency` | `usd` | Only `usd` is accepted; see Limitations |
| `webhook_source` | `synthetic` | `stripe_cli` = genuine events via `cli_triggers` (+ `cli_timeout_s`) |

`agentpay validate` rejects unknown fields, unknown fault types and invariant ids, malformed budgets, faults missing their required `on:`/`event:`, and non-USD currencies, naming the offending field.

---

## Invariants

Each scenario lists the invariants it asserts (`assert:`); if none are listed, all are checked.  These IDs match `agentpay invariants list` and `agentpay_harness/invariants/checks.py`.

| ID | Invariant | Typically violated by |
|----|-----------|-----------------------|
| **I1** | At most one successful charge per intent | Retrying without a stable idempotency key |
| **I2** | Total captured amount within budget | Check-then-act race on the budget |
| **I3** | Every charge has an approved intent in the ledger | Agent loses its own ledger write |
| **I4** | Idempotency key reuse returns the original response | Timestamp-derived or otherwise unstable keys |
| **I5** | Idempotency key mismatch returns a 400 error | Mutating amount/currency on retry |
| **I6** | Webhook events processed at most once | No event-ID deduplication |
| **I7** | Event processing is order-independent | Trusting arrival order for terminal states |
| **I8** | Webhook signatures verified before processing | Acting on a forged or stale (expired-signature) event |
| **I9** | Abort cleans up (no dangling captures or unrefunded charges) | Missing cancel / compensating action |
| **I10** | Rate-limit retries use exponential backoff | Retrying < 100 ms after a 429, or backoff that doesn't grow |
| **I11** | Ledger reconciles with Stripe records | Partial-failure amnesia, untracked funds |
| **I12** | Ledger records the captured amount, not the authorized amount | Ignoring `amount_received` after a partial capture |
| **I13** | Cumulative refunds never exceed the captured amount | Refunding the authorization, or booking a refund Stripe refused |

---

## Safety

> [!CAUTION]
> **This tool is designed exclusively for Stripe test mode.**

- The harness **refuses to start** if it detects a live key (`sk_live_*` or `rk_live_*`), and only accepts `sk_test_`, `rk_test_`, or `mock_` prefixes.
- Any API response with `livemode: true` aborts the run immediately.
- With no key configured, everything runs against a built-in offline mock.  `--base-url` points the client at a local [`stripe-mock`](https://github.com/stripe/stripe-mock).
- No real money is moved.  No real cards are charged.
- Scenario seeds make fault injection deterministic and reproducible.

> [!IMPORTANT]
> **Never commit `.env` files or raw API keys to version control.**  The harness reads keys from environment variables (`STRIPE_TEST_SECRET_KEY`, falling back to `STRIPE_API_KEY`) or `--stripe-key`; it does not load `.env` files itself.

---

## Tests for the Tester

`agentpay-harness` tests itself.  The `tests/` suite covers:

1. **Broken agents get caught, end to end** (`test_broken_agents.py`, `test_scenarios.py`, `test_features.py`, `test_end_to_end.py`) — the `ReferenceAgent` must pass every bundled scenario, and every one of the eleven broken agents must be caught by at least one scenario, on its matching invariant.  This is what proves the invariants actually detect bugs.
2. **Invariant correctness** (`test_invariants.py`, `test_more_invariants.py`) — synthetic logs: known-good must pass, known-bad must fail.
3. **The shrinker minimizes** (`test_shrink.py`) — a noisy 6-fault failing schedule shrinks to the single fault that causes it; faults that are needed together are both kept.
4. **Races are deterministic** — the same racy agent overspends under schedule `[0, 1, 0, 1]` and doesn't under `[0, 0, 1, 1]`.
5. **Scenario validation** (`test_validation.py`) — each kind of malformed scenario is rejected with an error naming the field.
6. **Stripe CLI mode** — offline, against a fake `stripe` executable that signs and forwards events like the real one; and for real (`@pytest.mark.integration`), when the CLI and a test key are available.
7. **Live-key rejection** (`test_safety.py`).

Run the suite and checks (what CI runs on Python 3.10, 3.11 and 3.12):

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy agentpay_harness/
pytest -m "not integration" --cov=agentpay_harness   # fails below 80% coverage
pytest -m integration                                # needs the Stripe CLI + STRIPE_TEST_SECRET_KEY
```

---

## Honest Limitations

- **Reference and broken agents only.**  The bundled adapters are test fixtures, not integrations with a real agent framework (LangChain, OpenAI Agents, etc.).  You implement `AgentUnderTest` for your own agent.
- **Simple task planning.**  `ReferenceAgent.plan()` understands tasks like "buy three packs at $75 each"; anything else becomes a single charge for the full budget.
- **Webhooks are synthetic by default.**  The driver generates and signs webhook events locally.  `webhook_source: stripe_cli` uses genuine Stripe deliveries instead, but in that mode the agent only observes events (it doesn't pay), and only duplicate and forged faults apply on top.
- **Some faults are offline only.**  `async_payment_failure` and `partial_capture` need the built-in mock; against Stripe test mode or stripe-mock they are ignored with a warning.
- **Capture and refund send no webhooks.**  After `settle` / `refund`, agents learn the result from the API response only.
- **stripe-mock fidelity.**  `stripe-mock` does not simulate webhook delivery, 3D Secure, or asynchronous settlement.  Some invariants are only fully meaningful against Stripe test mode.
- **USD only, enforced.**  Budgets and ledgers are summed in USD cents.  A scenario with `currency` other than `usd`, or an agent that plans a non-USD intent, fails immediately with `UnsupportedCurrencyError` instead of mis-summing.
- **Shared budgets need agent support.**  In multi-agent scenarios the harness passes a `SharedBudget` in `task.metadata["budget_store"]`; an agent that ignores it keeps a private budget and will overspend.
- **No persistent state.**  Each run is independent; no cross-run trend tracking.
- **Coverage gaps.**  These failure modes and 13 invariants are a starting point, not an exhaustive list.

---

## Contributing

Contributions are welcome!  Please:

1. Fork the repo and create a feature branch.
2. Install dev dependencies: `pip install -e ".[dev]"`
3. Ensure all checks pass:
   ```bash
   ruff check . && ruff format --check .
   mypy agentpay_harness/
   pytest -m "not integration" --cov=agentpay_harness
   ```
4. Add tests for any new invariants or fault types — ideally a new broken agent that the invariant catches.
5. Open a PR with a clear description of what and why.

---

## Releasing

Releases are published to PyPI by `.github/workflows/publish.yml` when a `v*` tag is pushed.  It checks the tag matches `__version__` in `agentpay_harness/__init__.py`, builds, smoke-tests the wheel in a clean venv, publishes via PyPI trusted publishing, then installs `agentpay-harness==<version>` from PyPI and runs `agentpay --help`.  One-time setup is described at the top of that workflow.

---

## Disclaimer

> **`agentpay-harness` is an independent open-source project.  It is NOT affiliated with, endorsed by, or sponsored by Stripe, Inc.**  "Stripe" is a registered trademark of Stripe, Inc.  This project uses Stripe's publicly available API and test-mode infrastructure in accordance with their terms of service.

---

## License

[MIT](LICENSE) © 2026 agentpay-harness contributors
