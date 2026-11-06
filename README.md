# agentpay-harness

<!-- Badges -->
[![CI](https://img.shields.io/github/actions/workflow/status/agentpay-harness/agentpay-harness/ci.yml?branch=main&label=CI)](https://github.com/agentpay-harness/agentpay-harness/actions)
[![PyPI](https://img.shields.io/pypi/v/agentpay-harness?color=blue)](https://pypi.org/project/agentpay-harness/)
[![Python](https://img.shields.io/pypi/pyversions/agentpay-harness)](https://pypi.org/project/agentpay-harness/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Fault-injection test harness for AI-agent-initiated Stripe payments.**

AI agents that spend real money need to be tested against the same failure modes that break human-written payment code — timeouts, retries, duplicate webhooks, race conditions, and more.  `agentpay-harness` provides a YAML-driven scenario runner that injects those faults deterministically and verifies a set of safety invariants after every run.

---

## What This Tests

The harness targets **7 failure modes** that commonly afflict AI-agent payment flows:

| # | Failure Mode | What Goes Wrong |
|---|---|---|
| 1 | **Timeout → Duplicate Charge** | Network timeout triggers a retry; agent creates two PaymentIntents for one purchase. |
| 2 | **Runaway Spend (Race)** | Concurrent purchase attempts each pass a budget check individually, but together exceed the limit. |
| 3 | **Webhook Duplication** | Stripe retries a webhook; agent processes `payment_intent.succeeded` twice and double-fulfills. |
| 4 | **Webhook Reordering** | `succeeded` arrives before `created`; agent crashes or creates inconsistent state. |
| 5 | **Stale Webhook Replay** | An old `payment_intent.created` event is replayed minutes later; agent re-opens a settled flow. |
| 6 | **Partial Capture Mismatch** | Captured amount differs from the expected amount; agent doesn't notice. |
| 7 | **Refund Exceeds Capture** | Agent issues a refund larger than the captured amount. |

---

## Quickstart

```bash
# Install
pip install agentpay-harness

# Copy and fill in your Stripe test-mode credentials
cp .env.example .env

# Run a scenario
agentpay run scenarios/duplicate-on-timeout.yaml

# Run all scenarios in a directory
agentpay run scenarios/

# Run with verbose output
agentpay run scenarios/duplicate-on-timeout.yaml --verbose

# List available invariants
agentpay invariants list
```

---

## Architecture

```
agentpay-harness/
├── pyproject.toml              # Package metadata, tool config
├── README.md
├── LICENSE
├── .env.example                # Template for Stripe test-mode keys
├── .gitignore
│
├── agentpay_harness/           # Main package
│   ├── __init__.py             # Version, top-level exports
│   ├── cli.py                  # Click CLI entrypoint
│   ├── runner.py               # Scenario execution engine
│   ├── config.py               # YAML scenario parsing & validation
│   │
│   ├── adapters/               # Agent framework integrations
│   │   └── __init__.py
│   │
│   ├── faults/                 # Fault injection middleware
│   │   ├── __init__.py
│   │   ├── delay.py            # delay_response
│   │   ├── drop.py             # drop_response
│   │   ├── error.py            # error_response
│   │   └── webhook_faults.py   # duplicate / reorder / replay
│   │
│   ├── stripeio/               # Managed Stripe API client
│   │   ├── __init__.py
│   │   ├── client.py           # HTTP transport + middleware stack
│   │   └── models.py           # Pydantic models for Stripe objects
│   │
│   ├── webhooks/               # Local webhook receiver
│   │   ├── __init__.py
│   │   ├── server.py           # ASGI webhook endpoint
│   │   └── proxy.py            # Fault proxy for webhook events
│   │
│   └── invariants/             # Post-run safety checks
│       ├── __init__.py
│       ├── budget.py           # I2, I3, I4, I11
│       ├── idempotency.py      # I1, I9
│       ├── webhooks.py         # I6, I7, I8
│       ├── lifecycle.py        # I5
│       └── refunds.py          # I10
│
├── scenarios/                  # Example YAML scenario files
│   ├── duplicate-on-timeout.yaml
│   ├── runaway-spend.yaml
│   └── webhook-chaos.yaml
│
├── docs/
│   └── FAILURES.md             # Detailed failure mode catalog
│
└── tests/                      # Test suite
    ├── conftest.py
    ├── test_invariants.py
    ├── test_faults.py
    └── test_scenarios.py
```

---

## Invariants

Every scenario specifies which invariants must hold after the run.  The harness checks each one and reports pass/fail with a human-readable explanation.

| ID | Invariant | Description |
|----|-----------|-------------|
| **I1** | Idempotency keys present | Every mutating Stripe API call carries an `Idempotency-Key` header. |
| **I2** | Budget gate | Agent checks remaining budget before initiating any charge. |
| **I3** | Atomic budget decrement | Budget is decremented with compare-and-swap, not read-then-write. |
| **I4** | Total ≤ budget | Total captured amount never exceeds the authorised budget. |
| **I5** | Terminal state reached | Every PaymentIntent reaches a terminal state (`succeeded`, `canceled`) within the timeout. |
| **I6** | Webhook idempotency | Duplicate webhook events do not cause duplicate side-effects. |
| **I7** | Event ordering tolerance | Agent does not assume webhook delivery order. |
| **I8** | Stale replay rejection | Webhook events older than the tolerance window are discarded. |
| **I9** | No duplicate intents | No two PaymentIntents correspond to the same logical purchase. |
| **I10** | Refund ≤ capture | Refund amount never exceeds captured amount for any PaymentIntent. |
| **I11** | Serialised budget mutations | Budget modifications are serialised — no concurrent read-modify-write races. |

---

## Safety

> [!CAUTION]
> **This tool is designed exclusively for Stripe test mode.**

- The harness **refuses to start** if it detects a live-mode secret key (`sk_live_*`).
- All PaymentIntents are created against Stripe's test-mode API or a local [`stripe-mock`](https://github.com/stripe/stripe-mock) instance.
- No real money is moved.  No real cards are charged.
- Webhook signature verification uses your test-mode webhook secret (`whsec_test_*`).
- Scenario seeds ensure deterministic fault injection for reproducible test runs.

> [!IMPORTANT]
> **Never commit `.env` files or raw API keys to version control.**  The `.gitignore` is pre-configured to exclude them, but always double-check before pushing.

---

## Tests for the Tester

`agentpay-harness` tests itself.  The `tests/` directory contains a full pytest suite that validates:

1. **Invariant correctness** — synthetic event logs are fed to each invariant checker; known-good logs must pass and known-bad logs must fail.  This ensures the invariants themselves don't have false positives or false negatives.

2. **Fault determinism** — given the same scenario seed, the fault injection layer must produce identical perturbations across runs.  Tests assert byte-level reproducibility.

3. **Scenario parsing** — malformed YAML, missing fields, out-of-range values, and unknown fault types must all produce clear, actionable error messages rather than silent misbehaviour.

4. **Live-key rejection** — the harness must raise `LiveKeyError` if a `sk_live_*` key is detected in the environment, regardless of how it's injected (env var, `.env` file, CLI flag).

Run the suite:

```bash
pip install -e ".[dev]"
pytest
```

---

## Honest Limitations

- **No real agent integration yet.**  The `adapters/` package defines the interface but ships no concrete adapters.  You'll need to implement one for your agent framework.
- **stripe-mock fidelity.**  `stripe-mock` does not simulate all Stripe behaviour (e.g., webhook delivery, 3D Secure flows, asynchronous settlement).  Some invariants can only be fully validated against Stripe's test-mode API.
- **Single-currency only.**  The budget system assumes USD.  Multi-currency support is not implemented.
- **No persistent state.**  Each scenario run is independent.  There is no cross-run state, trend tracking, or regression detection.
- **Webhook faults are synthetic.**  The harness injects webhook faults locally; it does not intercept real Stripe webhook deliveries.
- **Coverage gaps.**  The 7 failure modes and 11 invariants are a starting point, not an exhaustive list.  Payment systems have a long tail of subtle bugs.

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
4. Add tests for any new invariants or fault types.
5. Open a PR with a clear description of what and why.

---

## Disclaimer

> **`agentpay-harness` is an independent open-source project.  It is NOT affiliated with, endorsed by, or sponsored by Stripe, Inc.**  "Stripe" is a registered trademark of Stripe, Inc.  This project uses Stripe's publicly available API and test-mode infrastructure in accordance with their terms of service.

---

## License

[MIT](LICENSE) © 2026 agentpay-harness contributors
