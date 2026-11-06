# Failure Mode Catalog

This document describes **10 failure modes** that affect AI-agent payment flows. Each is reproduced by at least one bundled scenario, and each has a deliberately broken agent in `agentpay_harness/adapters/broken.py` that the scenario catches.

Invariant IDs below refer to the checks in `agentpay_harness/invariants/checks.py` (run `agentpay invariants list`).  Each entry covers what goes wrong in production, how the harness reproduces it, how to fix it, and which invariants catch it.

---

## Table of Contents

1. [F1 — Timeout → Duplicate Charge](#f1--timeout--duplicate-charge)
2. [F2 — Runaway Spend (Concurrent Budget Overrun)](#f2--runaway-spend-concurrent-budget-overrun)
3. [F3 — Webhook Duplication](#f3--webhook-duplication)
4. [F4 — Webhook Reordering](#f4--webhook-reordering)
5. [F5 — Stale Webhook Replay](#f5--stale-webhook-replay)
6. [F6 — Partial Capture Mismatch](#f6--partial-capture-mismatch)
7. [F7 — Refund Exceeds Capture](#f7--refund-exceeds-capture)
8. [F8 — Forged Webhook](#f8--forged-webhook)
9. [F9 — Abandoned Authorization](#f9--abandoned-authorization)
10. [F10 — Rate-Limit Hammering](#f10--rate-limit-hammering)

---

## F1 — Timeout → Duplicate Charge

### Real-World Description

An AI agent calls `POST /v1/payment_intents` to create a $50 charge.  The Stripe API processes the request and creates the PaymentIntent, but the HTTP response is delayed beyond the agent's timeout threshold (e.g., 3 seconds).  The agent's HTTP client raises a timeout exception.  The agent, seeing no confirmation, retries the request — creating a **second** PaymentIntent for the same purchase.  The customer is charged $100 instead of $50.

This is the single most common payment bug in distributed systems.  Stripe provides idempotency keys specifically to prevent it, but agents must actually *use* them.

### What Happens

1. Agent sends `POST /v1/payment_intents` with `amount=5000`.
2. Stripe creates `pi_ABC` and begins sending the response.
3. The harness drops the response on the wire (`drop_response` fault) — Stripe created the PaymentIntent, but the agent sees a timeout.
4. Agent's HTTP client times out after 3 seconds.
5. Agent retries: sends `POST /v1/payment_intents` with `amount=5000` again.
6. Stripe creates `pi_DEF` — a completely separate PaymentIntent.
7. Both are confirmed and captured.  Customer pays $100.

### Minimal Reproduction

```bash
agentpay run scenarios/duplicate-on-timeout.yaml
```

**Scenario file:** `scenarios/duplicate-on-timeout.yaml`
**Faults:** `drop_response` on the 1st `POST /v1/payment_intents`, `rate_limit` (429) on the 2nd, plus duplicated and reordered webhooks.

### The Fix

- **Always attach an idempotency key** to every mutating Stripe API call.  Use a deterministic key derived from the logical operation (e.g., `sha256(agent_id + task_id + operation_index)`).
- **Retry with the same idempotency key.**  Stripe will return the original response instead of creating a duplicate.
- **Verify before retrying.**  Before retrying a timed-out create, list recent PaymentIntents to check if the original succeeded.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I1** | At most one successful charge per intent — a retry with a fresh key creates a second PaymentIntent. |
| **I4** | Reusing an idempotency key returns the original response. |
| **I11** | The agent's ledger reconciles with what Stripe actually recorded. |

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

**Two agents, one budget — a deterministic race:**

```bash
agentpay run scenarios/budget-race.yaml --agent agentpay_harness.adapters.broken.BrokenBudgetAgent
```

`scenarios/budget-race.yaml` runs two agent instances against one shared $100 budget (`agents: 2`), each wanting $75.  The `interleave` fault with `schedule: [0, 1, 0, 1]` makes agent 0 read the balance, then agent 1 read it, then each write: both saw "$0 spent", both pay, $150 leaves a $100 budget.  The same agent under `[0, 0, 1, 1]` does not overspend, so the failure comes from the interleaving, not from timing luck.  A correct agent uses `SharedBudget.try_reserve()` (check-and-reserve in one step), which passes under every schedule.

### The Fix

- **Use atomic budget operations.**  Implement compare-and-swap (CAS) or database-level locking so that budget checks and decrements are a single atomic operation.
- **Serialise budget mutations.**  Use a mutex, database transaction, or queue to ensure only one charge is evaluated at a time.
- **Pre-authorise, then capture.**  Create PaymentIntents in `manual` capture mode.  Decrement the budget atomically when creating the intent (not when capturing).  This way, the budget is reserved before the capture delay.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I2** | Total captured amount stays within the budget. |
| **I3** | Every charge has an approved intent in the agent's ledger. |
| **I11** | The agent's ledger reconciles with Stripe. |

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

1. Agent buys two items.  The first is an async payment: Stripe accepts it as `processing` (`async_payment_failure` fault).
2. The first payment then fails.  Stripe reports this **only** by a `payment_intent.payment_failed` webhook.
3. The harness shuffles deliveries within a window of 4 (`reorder_webhooks`), so `payment_failed` arrives after newer events for the second purchase.
4. An agent that drops events "older" than the last one it saw never learns the first payment failed.
5. Its ledger still shows the money as captured; Stripe says `requires_payment_method`.

### Minimal Reproduction

```bash
agentpay run scenarios/webhook-out-of-order.yaml --agent agentpay_harness.adapters.broken.BrokenWebhookOrderAgent
```

**Scenario file:** `scenarios/webhook-out-of-order.yaml`
**Faults:** `async_payment_failure` on the 1st `POST /v1/payment_intents`, `reorder_webhooks` with window 4.

### The Fix

- **Fetch state from Stripe, not local cache.**  When processing any webhook event, call `stripe.PaymentIntent.retrieve(pi_id)` to get the current state instead of relying on local state built from prior webhooks.
- **Accept events in any order.**  Design your state machine to handle transitions from any state, not just the expected predecessor.
- **Never discard an event just because it is older.**  Use the event's `created` timestamp to decide which *state* wins for one object, but still process every event; an older event for one PaymentIntent says nothing about another.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I7** | The ledger's final state matches Stripe's terminal state for every PaymentIntent, whatever the delivery order. |
| **I11** | The ledger reconciles with Stripe (no untracked or phantom funds). |

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
agentpay run scenarios/forged-webhook.yaml
```

**Scenario files:** `scenarios/webhook-chaos.yaml` (30 s replay — still within tolerance, so deduplication is what matters) and `scenarios/forged-webhook.yaml` (600 s replay — the original signature has expired and must be rejected).
**Fault:** `replay_webhook` on `payment_intent.created`.  The replay carries the **original** `Stripe-Signature` header, as a captured-and-replayed request would.

**Clock skew:** `scenarios/clock-skew.yaml` shifts signature timestamps instead (`clock_skew` fault): −120 s on `payment_intent.created` (inside the 300 s tolerance: must be accepted) and +900 s on `payment_intent.succeeded` (outside: must be rejected).

```bash
agentpay run scenarios/clock-skew.yaml --agent agentpay_harness.adapters.broken.BrokenWebhookVerifyAgent
```

### The Fix

- **Verify the signature timestamp.**  Stripe's `verify_signature` rejects headers older than the tolerance (300 s by default).  Stripe re-signs its own retries, so a stale signature means a replayed request.
- **Check current object state.**  Before processing a `created` event, verify that the PaymentIntent is still in the `requires_payment_method` or `requires_confirmation` state.  If it's already `succeeded` or `canceled`, ignore the event.
- **Idempotent state transitions.**  Ensure that transitioning from `succeeded` → `created` is a no-op, not a state reset.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I8** | Webhook signatures are verified, with the timestamp inside the tolerance window, before processing. |

---

## F6 — Partial Capture Mismatch

### Real-World Description

An agent authorizes $50.00 with `capture_method=manual` and captures later.  Stripe can capture less than was authorized (the merchant passes `amount_to_capture`, an order is partly fulfilled, an authorization is partly released).  The capture response says what really moved in `amount_received`.  An agent that books the original $50.00 overstates its spend, under-reports its remaining budget, and later tries to refund money it never received (F7).

### What Happens

1. Agent creates a PaymentIntent for $50.00 with `capture_method=manual` (`requires_capture`).
2. The scenario settles (`settle: true`), and the agent calls `POST /v1/payment_intents/pi_.../capture`.
3. The `partial_capture` fault makes Stripe capture only 60%: `amount_received = 3000`.
4. A correct agent books $30.00 and returns the unused $20.00 reservation to its budget.
5. A broken agent books $50.00.

### Minimal Reproduction

```bash
agentpay run scenarios/partial-capture.yaml --agent agentpay_harness.adapters.broken.BrokenCaptureAgent
```

**Scenario file:** `scenarios/partial-capture.yaml`
**Fault:** `partial_capture` with `percent: 60` (offline mock only).

### The Fix

- **Read `amount_received` from the capture response**, never the intent's original `amount`.
- **Release the difference.**  The authorization reserved $50.00 of budget; only $30.00 was spent.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I12** | For every captured PaymentIntent, the ledger amount equals Stripe's `amount_received`. |

---

## F7 — Refund Exceeds Capture

### Real-World Description

After the partial capture above, the purchase is refunded.  An agent that refunds the *authorized* $50.00 asks Stripe for more than the $30.00 it captured.  Stripe rejects it (`amount_too_large`), but an agent that doesn't check the response books a $50.00 refund anyway: its ledger now says $50.00 came back when $0.00 did.  The same invariant also catches repeated partial refunds that add up to more than the capture.

### What Happens

1. Same partial capture as F6 ($50.00 authorized, $30.00 captured).
2. The scenario refunds (`refund: true`), and the agent calls `POST /v1/refunds`.
3. Broken agent: asks for $50.00, Stripe answers 400 `amount_too_large`, the agent books a $50.00 refund anyway.
4. Correct agent: refunds exactly the $30.00 captured and books only what Stripe confirms.

### Minimal Reproduction

```bash
agentpay run scenarios/refund-overflow.yaml --agent agentpay_harness.adapters.broken.BrokenRefundAgent
```

**Scenario file:** `scenarios/refund-overflow.yaml`
**Fault:** `partial_capture` with `percent: 60`, then `refund: true`.

### The Fix

- **Refund at most `amount_received - amount_refunded`.**
- **Book a refund only after Stripe confirms it** (2xx, `status` `succeeded` or `pending`).
- **Use an idempotency key on refunds**, so a timed-out refund retried doesn't refund twice.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I13** | Per PaymentIntent, refunds confirmed by Stripe (summed) and refunds claimed by the ledger never exceed the captured amount, and the ledger never claims more than Stripe refunded. |

---

## F8 — Forged Webhook

### Real-World Description

Webhook endpoints are public URLs.  Anyone can POST a JSON body that looks like `payment_intent.succeeded`.  An agent that trusts the body without verifying the `Stripe-Signature` header can be tricked into fulfilling an order, or crediting a balance, for money that never arrived.

### What Happens

1. Agent buys one item; the real `payment_intent.succeeded` is delivered.
2. The harness also delivers a forged `payment_intent.succeeded` for a PaymentIntent that doesn't exist, signed with the wrong secret (`forged_webhook` fault).
3. An agent that skips verification books the forged payment in its ledger.

### Minimal Reproduction

```bash
agentpay run scenarios/forged-webhook.yaml --agent agentpay_harness.adapters.broken.BrokenWebhookVerifyAgent
```

### The Fix

- Verify every event with `verify_signature(event.payload, event.signature_header, secret)` before acting on it (the harness signs with `HARNESS_WEBHOOK_SECRET`).
- Treat the event body as a hint: re-fetch the PaymentIntent from Stripe before moving money.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I8** | No ledger entry was caused by, or names a PaymentIntent known only from, an event with an invalid or stale signature. |

---

## F9 — Abandoned Authorization

### Real-World Description

An agent authorizes payments with `capture_method=manual` (e.g. to hold seats), then the task is canceled.  If it simply stops, the authorizations stay on the customer's card for days, and may still be captured later.

### What Happens

1. Agent places two $40 authorizations (`requires_capture`).
2. The scenario aborts the task (`abort: true`), and the driver calls `agent.abort(client)`.
3. An agent that does nothing on abort leaves both PaymentIntents in `requires_capture`.

### Minimal Reproduction

```bash
agentpay run scenarios/abort-cleanup.yaml --agent agentpay_harness.adapters.broken.BrokenAbortAgent
```

### The Fix

- On abort, cancel every PaymentIntent you created that is not yet captured, and release its budget reservation.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I9** | After the run, no PaymentIntent is still in `requires_capture`. |

---

## F10 — Rate-Limit Hammering

### Real-World Description

Stripe returns 429 when a client sends too many requests.  An agent that retries immediately, or at a fixed short interval, keeps the limit tripped and can turn a brief throttle into an outage.

### What Happens

1. The first two `POST /v1/payment_intents` calls return 429 (`rate_limit` fault, `times: 2`).
2. A correct agent waits at least 100 ms, then longer on the next retry (exponential backoff with jitter).
3. A broken agent retries immediately.

### Minimal Reproduction

```bash
agentpay run scenarios/rate-limit-storm.yaml --agent agentpay_harness.adapters.broken.BrokenBackoffAgent
```

### The Fix

- Use exponential backoff with jitter that keeps a guaranteed minimum (e.g. "equal jitter": `cap/2 + random(0, cap/2)`).
- Treat `Retry-After` as a floor, not a replacement for backoff.

### Invariants That Catch It

| Invariant | What It Checks |
|-----------|----------------|
| **I10** | Every retry after a 429 waits ≥ 100 ms, and the last wait in a run of 429s is at least the first. |

---

## Summary Matrix

| Failure | Scenario | Faults | Invariants |
|---------|----------|--------|------------|
| F1 — Timeout Duplicate | `duplicate-on-timeout.yaml` | `drop_response`, `rate_limit` | I1, I4, I11 |
| F2 — Runaway Spend | `runaway-spend.yaml`, `budget-race.yaml` | `delay_response`, `interleave` | I2, I3, I11 |
| F3 — Webhook Duplication | `webhook-chaos.yaml` | `duplicate_webhook` | I6 |
| F4 — Webhook Reordering | `webhook-out-of-order.yaml` | `async_payment_failure`, `reorder_webhooks` | I7, I11 |
| F5 — Stale Replay / Clock Skew | `forged-webhook.yaml`, `clock-skew.yaml`, `webhook-chaos.yaml` | `replay_webhook`, `clock_skew` | I8, I6 |
| F6 — Partial Capture | `partial-capture.yaml` | `partial_capture` | I12 |
| F7 — Refund Overflow | `refund-overflow.yaml` | `partial_capture` + `refund: true` | I13 |
| F8 — Forged Webhook | `forged-webhook.yaml` | `forged_webhook` | I8 |
| F9 — Abandoned Authorization | `abort-cleanup.yaml` | `capture_method: manual`, `abort: true` | I9 |
| F10 — Rate-Limit Hammering | `rate-limit-storm.yaml` | `rate_limit` | I10 |
