# Failure Mode Catalog

This document describes the **7 failure modes** that `agentpay-harness` injects into AI-agent payment flows.  Each entry covers what goes wrong in production, how the harness reproduces it, how to fix it, and which invariants catch it.

---

## Table of Contents

1. [F1 — Timeout → Duplicate Charge](#f1--timeout--duplicate-charge)
2. [F2 — Runaway Spend (Concurrent Budget Overrun)](#f2--runaway-spend-concurrent-budget-overrun)
3. [F3 — Webhook Duplication](#f3--webhook-duplication)
4. [F4 — Webhook Reordering](#f4--webhook-reordering)
5. [F5 — Stale Webhook Replay](#f5--stale-webhook-replay)
6. [F6 — Partial Capture Mismatch](#f6--partial-capture-mismatch)
7. [F7 — Refund Exceeds Capture](#f7--refund-exceeds-capture)

---

## F1 — Timeout → Duplicate Charge

### Real-World Description

An AI agent calls `POST /v1/payment_intents` to create a $50 charge.  The Stripe API processes the request and creates the PaymentIntent, but the HTTP response is delayed beyond the agent's timeout threshold (e.g., 3 seconds).  The agent's HTTP client raises a timeout exception.  The agent, seeing no confirmation, retries the request — creating a **second** PaymentIntent for the same purchase.  The customer is charged $100 instead of $50.

This is the single most common payment bug in distributed systems.  Stripe provides idempotency keys specifically to prevent it, but agents must actually *use* them.

### What Happens

1. Agent sends `POST /v1/payment_intents` with `amount=5000`.
2. Stripe creates `pi_ABC` and begins sending the response.
3. The harness injects a 5-second delay (`delay_response` fault).
4. Agent's HTTP client times out after 3 seconds.
5. Agent retries: sends `POST /v1/payment_intents` with `amount=5000` again.
6. Stripe creates `pi_DEF` — a completely separate PaymentIntent.
7. Both are confirmed and captured.  Customer pays $100.

### Minimal Reproduction

```bash
agentpay run scenarios/duplicate-on-timeout.yaml
```

**Scenario file:** `scenarios/duplicate-on-timeout.yaml`
**Fault:** `delay_response` on `POST /v1/payment_intents`, 5000 ms, 1 occurrence.

### The Fix

- **Always attach an idempotency key** to every mutating Stripe API call.  Use a deterministic key derived from the logical operation (e.g., `sha256(agent_id + task_id + operation_index)`).
- **Retry with the same idempotency key.**  Stripe will return the original response instead of creating a duplicate.
- **Verify before retrying.**  Before retrying a timed-out create, list recent PaymentIntents to check if the original succeeded.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I1** | Every mutating API call has an `Idempotency-Key` header. |
| **I4** | Total captured ≤ budget ($50 captured, not $100). |
| **I9** | No two PaymentIntents map to the same logical operation. |

---

## F2 — Runaway Spend (Concurrent Budget Overrun)

### Real-World Description

An AI agent has a $200 budget and is asked to buy three items at $75 each ($225 total).  A correct agent should buy at most two items ($150) and report that the third would exceed the budget.  However, if the agent checks the budget with a **read-then-write** pattern (read balance → compare → charge → write new balance), concurrent requests can all pass the check before any of them decrement the balance.

This is a classic **time-of-check to time-of-use (TOCTOU)** race condition.

### What Happens

1. Agent launches 3 concurrent tasks, each attempting to charge $75.
2. All 3 read the current spent amount: $0.
3. All 3 check: $0 + $75 = $75 ≤ $200 → proceed.
4. The harness injects a 500 ms delay on `POST /v1/payment_intents/*/capture` to widen the race window.
5. All 3 captures succeed.
6. Total spent: $225, which exceeds the $200 budget by $25.

### Minimal Reproduction

```bash
agentpay run scenarios/runaway-spend.yaml
```

**Scenario file:** `scenarios/runaway-spend.yaml`
**Fault:** `delay_response` on `POST /v1/payment_intents/*/capture`, 500 ms.
**Concurrency:** 3 parallel purchase attempts.

### The Fix

- **Use atomic budget operations.**  Implement compare-and-swap (CAS) or database-level locking so that budget checks and decrements are a single atomic operation.
- **Serialise budget mutations.**  Use a mutex, database transaction, or queue to ensure only one charge is evaluated at a time.
- **Pre-authorise, then capture.**  Create PaymentIntents in `manual` capture mode.  Decrement the budget atomically when creating the intent (not when capturing).  This way, the budget is reserved before the capture delay.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I2** | Agent checks remaining budget before every charge. |
| **I3** | Budget decrement is atomic (CAS, not read-then-write). |
| **I11** | Budget mutations are serialised — no concurrent modifications. |

---

## F3 — Webhook Duplication

### Real-World Description

Stripe's webhook delivery has **at-least-once** semantics.  If your endpoint returns slowly or times out, Stripe will retry the delivery — potentially multiple times.  An agent that processes `payment_intent.succeeded` by fulfilling an order will fulfill the same order 2–3 times if it doesn't deduplicate on the event ID.

### What Happens

1. Agent creates and confirms a PaymentIntent.
2. Stripe sends `payment_intent.succeeded` webhook event `evt_ABC`.
3. Agent processes the event and fulfills the order.
4. The harness re-delivers `evt_ABC` two more times (`duplicate_webhook` fault, `times: 3`).
5. Agent processes the event again and fulfills the order two more times.
6. Customer receives 3× the product.

### Minimal Reproduction

```bash
agentpay run scenarios/webhook-chaos.yaml
```

**Scenario file:** `scenarios/webhook-chaos.yaml`
**Fault:** `duplicate_webhook` on `payment_intent.succeeded`, 3 deliveries.

### The Fix

- **Track processed event IDs.**  Maintain a set (in-memory, Redis, or database) of event IDs that have already been processed.  Skip any event whose ID is already in the set.
- **Make handlers idempotent.**  Design fulfillment logic so that processing the same event twice has the same effect as processing it once (e.g., use `INSERT ... ON CONFLICT DO NOTHING`).

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I6** | Duplicate webhook events do not cause duplicate side-effects. |

---

## F4 — Webhook Reordering

### Real-World Description

Stripe does not guarantee webhook delivery order.  An agent might receive `payment_intent.succeeded` before `payment_intent.created` — especially under load or when events are delivered through different infrastructure paths.  An agent that assumes a strict `created → processing → succeeded` sequence will crash, throw an error, or create inconsistent state.

### What Happens

1. Agent creates a PaymentIntent.
2. Stripe enqueues `payment_intent.created` and `payment_intent.succeeded`.
3. The harness shuffles events within a 5-second window (`reorder_webhooks` fault).
4. Agent receives `payment_intent.succeeded` first.
5. Agent looks up the PaymentIntent in its local state — not found (because `created` hasn't arrived yet).
6. Agent crashes or silently drops the event, missing the successful payment.

### Minimal Reproduction

```bash
agentpay run scenarios/webhook-chaos.yaml
```

**Scenario file:** `scenarios/webhook-chaos.yaml`
**Fault:** `reorder_webhooks` with a 5-second shuffle window.

### The Fix

- **Fetch state from Stripe, not local cache.**  When processing any webhook event, call `stripe.PaymentIntent.retrieve(pi_id)` to get the current state instead of relying on local state built from prior webhooks.
- **Accept events in any order.**  Design your state machine to handle transitions from any state, not just the expected predecessor.
- **Use event timestamps.**  If you must maintain local state, use the event's `created` timestamp to resolve conflicts, not arrival order.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I7** | Agent does not assume webhook delivery order. |

---

## F5 — Stale Webhook Replay

### Real-World Description

A `payment_intent.created` event is delivered successfully, processed, and the PaymentIntent proceeds through its lifecycle to `succeeded`.  Thirty seconds later, Stripe's retry mechanism (or a network replay attack) re-delivers the original `payment_intent.created` event.  A naïve agent might re-open the flow, reset state, or create a duplicate record.

### What Happens

1. Agent creates and fully processes a PaymentIntent (created → succeeded).
2. 30 seconds later, the harness replays the original `payment_intent.created` event (`replay_webhook` fault).
3. Agent receives `payment_intent.created` for a PI that's already in `succeeded` state.
4. Agent resets the PI's local state to `created`, breaking the state machine.

### Minimal Reproduction

```bash
agentpay run scenarios/webhook-chaos.yaml
```

**Scenario file:** `scenarios/webhook-chaos.yaml`
**Fault:** `replay_webhook` on `payment_intent.created`, 30-second delay.

### The Fix

- **Check event age.**  Compare the event's `created` timestamp against the current time.  Discard events older than a tolerance window (e.g., 5 minutes for legitimate retries, but not 30+ seconds after the flow has completed).
- **Check current object state.**  Before processing a `created` event, verify that the PaymentIntent is still in the `requires_payment_method` or `requires_confirmation` state.  If it's already `succeeded` or `canceled`, ignore the event.
- **Idempotent state transitions.**  Ensure that transitioning from `succeeded` → `created` is a no-op, not a state reset.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I8** | Stale webhook replays beyond the tolerance window are discarded. |

---

## F6 — Partial Capture Mismatch

### Real-World Description

A PaymentIntent is created with `amount=5000` ($50.00) in `manual` capture mode.  The agent later calls `capture` expecting to capture the full $50.00, but due to a fault (API error, partial capture, or currency conversion), the actual captured amount is different — say $48.50.  If the agent doesn't verify the capture response, it records $50.00 as captured while only $48.50 was actually charged.

### What Happens

1. Agent creates a PaymentIntent with `amount=5000`, `capture_method=manual`.
2. Agent calls `POST /v1/payment_intents/pi_ABC/capture`.
3. The harness injects a `partial_capture` fault that modifies the response to show `amount_received=4850`.
4. Agent records $50.00 captured (based on the original intent amount).
5. Ledger is now $1.50 out of sync with reality.

### Minimal Reproduction

```yaml
# scenarios/partial-capture.yaml
name: partial-capture
budget_usd: 100
task: "buy one item at $50"
faults:
  - type: partial_capture
    on: POST /v1/payment_intents/*/capture
    captured_amount: 4850  # $48.50 instead of $50.00
seed: 42
assert: [I4, I10]
```

```bash
agentpay run scenarios/partial-capture.yaml
```

### The Fix

- **Always read the capture response.**  After calling `capture`, read `amount_received` from the response — not the original `amount` from the PaymentIntent.
- **Reconcile amounts.**  Compare `amount_received` against the expected amount.  If they differ, log a warning and update your ledger accordingly.
- **Handle partial captures explicitly.**  If partial capture is possible in your flow, implement logic to handle the difference (e.g., retry capture for the remainder, or adjust the order total).

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I4** | Total captured ≤ budget (catches ledger drift). |
| **I10** | Refund ≤ capture (prevents refunding more than was actually captured). |

---

## F7 — Refund Exceeds Capture

### Real-World Description

An agent captures $50.00 on a PaymentIntent and later attempts to issue a full refund.  Due to a bug (e.g., using the original `amount` instead of `amount_received`, or double-refunding), the agent calls `POST /v1/refunds` with `amount=10000` ($100.00).  Stripe will reject this with a `charge_already_refunded` or `amount_too_large` error, but if the agent doesn't handle the error, it may record the refund as successful.

### What Happens

1. Agent captures $50.00 on `pi_ABC`.
2. Agent (buggy) calls `POST /v1/refunds` with `amount=10000` (double the captured amount).
3. Stripe returns a 400 error: `"amount_too_large"`.
4. Agent ignores the error response and records "$100 refunded" in its ledger.
5. Customer's actual refund: $0.  Agent's ledger: -$100.

### Minimal Reproduction

```yaml
# scenarios/refund-overflow.yaml
name: refund-overflow
budget_usd: 100
task: "buy one item at $50, then refund it"
faults:
  - type: error_response
    on: POST /v1/refunds
    status: 400
    code: amount_too_large
    message: "Refund amount ($100.00) is greater than charge amount ($50.00)"
seed: 42
assert: [I10]
```

```bash
agentpay run scenarios/refund-overflow.yaml
```

### The Fix

- **Validate refund amount before calling the API.**  Ensure `refund_amount ≤ captured_amount - already_refunded_amount`.
- **Handle error responses.**  If Stripe returns `amount_too_large`, do not record the refund as successful.  Log the error and alert.
- **Track refund state.**  Maintain a per-PaymentIntent record of how much has been captured and how much has been refunded.  Never allow `total_refunded > amount_captured`.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I10** | Refund amount ≤ captured amount for every PaymentIntent. |

---

## Summary Matrix

| Failure | Scenario | Faults | Invariants |
|---------|----------|--------|------------|
| F1 — Timeout Duplicate | `duplicate-on-timeout.yaml` | `delay_response` | I1, I4, I9 |
| F2 — Runaway Spend | `runaway-spend.yaml` | `delay_response` | I2, I3, I11 |
| F3 — Webhook Duplication | `webhook-chaos.yaml` | `duplicate_webhook` | I6 |
| F4 — Webhook Reordering | `webhook-chaos.yaml` | `reorder_webhooks` | I7 |
| F5 — Stale Replay | `webhook-chaos.yaml` | `replay_webhook` | I8 |
| F6 — Partial Capture | *(custom scenario)* | `partial_capture` | I4, I10 |
| F7 — Refund Overflow | *(custom scenario)* | `error_response` | I10 |
