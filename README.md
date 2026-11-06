# agentpay-harness

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Fault-injection test harness for AI-agent-initiated Stripe payments.**

AI agents that spend real money need to be tested against the same failure modes that break human-written payment code — timeouts, retries, duplicate webhooks, race conditions, and more.  `agentpay-harness` provides a YAML-driven scenario runner that injects those faults deterministically and verifies a set of safety invariants after every run.

---

## What This Tests

The harness targets these failure modes in AI-agent payment flows (details in [docs/FAILURES.md](docs/FAILURES.md)):

| # | Failure Mode | What Goes Wrong | Status |
|---|---|---|---|
| F1 | **Timeout → Duplicate Charge** | A dropped response triggers a retry; agent creates two PaymentIntents for one purchase. | `duplicate-on-timeout.yaml` |
| F2 | **Runaway Spend (Race)** | Concurrent purchases each pass a budget check individually, but together exceed the limit. | `runaway-spend.yaml` |
| F3 | **Webhook Duplication** | Stripe retries a webhook; agent processes `payment_intent.succeeded` twice. | `webhook-chaos.yaml` |
| F4 | **Webhook Reordering** | Events arrive out of order; an agent that drops "older" events misses an async payment failure. | `webhook-out-of-order.yaml` |
| F5 | **Stale Webhook Replay** | An old event is replayed with its original, now-expired signature. | `forged-webhook.yaml`, `webhook-chaos.yaml` |
| F6 | **Partial Capture Mismatch** | Captured amount differs from the expected amount; agent doesn't notice. | Planned |
| F7 | **Refund Exceeds Capture** | Agent issues a refund larger than the captured amount. | Planned |
| F8 | **Forged Webhook** | An event signed with the wrong secret claims a payment succeeded. | `forged-webhook.yaml` |
| F9 | **Abandoned Authorization** | Task is aborted after authorizing; holds are left on the card. | `abort-cleanup.yaml` |
| F10 | **Rate-Limit Hammering** | Agent retries 429s immediately instead of backing off. | `rate-limit-storm.yaml` |

---

## Quickstart

```bash
# Install from source (not yet published to PyPI)
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
```

To test your own agent, implement the `AgentUnderTest` protocol (`plan`, `pay`, `on_event`, `ledger`) from `agentpay_harness/adapters/protocol.py` and pass its dotted path to `--agent`.

---

## Architecture

```
agentpay_harness/
├── cli.py                 # Click CLI: run, replay, validate, invariants list
├── driver.py              # HarnessDriver — runs one scenario against one agent
├── scenario.py            # YAML scenario loading & validation
├── types.py               # Shared dataclasses / enums (Task, Intent, FaultConfig, ...)
├── safety.py              # Live-key refusal, livemode response guard
├── ledger.py              # Ledger ↔ Stripe reconciliation
├── report.py              # Rich report output, trace save/load
├── shrink.py              # Fault-schedule shrinker (minimal repro)
│
├── adapters/
│   ├── protocol.py        # AgentUnderTest / StripeClient protocols
│   ├── reference.py       # ReferenceAgent — correct baseline that passes every scenario
│   └── broken.py          # 9 deliberately broken agents, one per bug class
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
    └── checks.py          # I1–I11

scenarios/                 # 7 scenarios, one or more per failure mode
docs/FAILURES.md           # Failure mode catalog
tests/                     # pytest suite (see "Tests for the Tester")
```

### How a run works

1. The agent **plans** the task into intents, then **pays** each one through a recording Stripe client that applies request faults.
2. Webhook events (`payment_intent.created`, `.succeeded`, `.payment_failed`) are **queued** with increasing timestamps, then **delivered** after the payments the way Stripe delivers them, asynchronously. Each delivery is signed (`StripeEvent.payload` + `StripeEvent.signature_header`) with `HARNESS_WEBHOOK_SECRET`. Duplicates, reordering, stale replays and forgeries are applied at this stage.
3. If the scenario sets `abort: true`, the driver calls the agent's `abort(client)`.
4. The invariants are checked against the recorded requests, deliveries and the agent's ledger.

### Fault types

| Fault | Applies to | Effect |
|---|---|---|
| `drop_response` | request | Stripe processes the request; the response is lost (timeout) |
| `rate_limit` | request | 429 response |
| `server_error` | request | 5xx response |
| `delay_response` | request | Response delayed by `ms` |
| `duplicate_request` | request | Request sent twice |
| `async_payment_failure` | request | PaymentIntent comes back `processing`, then fails; only a webhook says so (offline mode only) |
| `duplicate_webhook` | webhook | Event delivered `times` times |
| `reorder_webhooks` | webhook | Deliveries shuffled within `window` (always out of order) |
| `replay_webhook` | webhook | Event re-delivered later with its original signature, `delay_s` old (stale past 300s) |
| `forged_webhook` | webhook | Extra event for a fake PaymentIntent, signed with the wrong secret |
| `clock_skew` | — | Parsed but not yet implemented |

Scenario files can also set `capture_method: manual` (authorize now, capture later) and `abort: true`.

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

1. **Broken agents get caught, end to end** (`test_broken_agents.py`, `test_scenarios.py`) — the `ReferenceAgent` must pass every bundled scenario, and each of the nine broken agents must trip its matching invariant in a real scenario run.  This is what proves the invariants actually detect bugs.
2. **Invariant correctness** (`test_invariants.py`, `test_more_invariants.py`) — synthetic logs for I1–I11: known-good must pass, known-bad must fail.  Also covers ledger reconciliation and the shrinker.
3. **Live-key rejection** (`test_safety.py`) — live secret and restricted keys are refused by the validator and the recording client; `livemode: true` responses are flagged.

Run the suite and checks:

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

---

## Honest Limitations

- **Reference and broken agents only.**  The bundled adapters are test fixtures, not integrations with a real agent framework (LangChain, OpenAI Agents, etc.).  You implement `AgentUnderTest` for your own agent.
- **Simple task planning.**  `ReferenceAgent.plan()` understands tasks like "buy three packs at $75 each"; anything else becomes a single charge for the full budget.
- **Webhooks are synthetic.**  The driver generates and signs webhook events locally from payment results; it does not intercept real Stripe deliveries.
- **Async failures are offline only.**  `async_payment_failure` needs the built-in mock; against Stripe test mode or stripe-mock it is ignored with a warning.
- **stripe-mock fidelity.**  `stripe-mock` does not simulate webhook delivery, 3D Secure, or asynchronous settlement.  Some invariants are only fully meaningful against Stripe test mode.
- **F6 / F7 not implemented.**  Partial-capture and refund-overflow faults, and a refund ≤ capture invariant, are designed in `docs/FAILURES.md` but not built.
- **Single currency.**  Budgets assume USD.
- **No persistent state.**  Each run is independent; no cross-run trend tracking.
- **Coverage gaps.**  These failure modes and 11 invariants are a starting point, not an exhaustive list.

---

## Contributing

Contributions are welcome!  Please:

1. Fork the repo and create a feature branch.
2. Install dev dependencies: `pip install -e ".[dev]"`
3. Ensure all checks pass:
   ```bash
   ruff check .
   mypy agentpay_harness/
   pytest --cov
   ```
4. Add tests for any new invariants or fault types — ideally a new broken agent that the invariant catches.
5. Open a PR with a clear description of what and why.

---

## Disclaimer

> **`agentpay-harness` is an independent open-source project.  It is NOT affiliated with, endorsed by, or sponsored by Stripe, Inc.**  "Stripe" is a registered trademark of Stripe, Inc.  This project uses Stripe's publicly available API and test-mode infrastructure in accordance with their terms of service.

---

## License

[MIT](LICENSE) © 2026 agentpay-harness contributors
