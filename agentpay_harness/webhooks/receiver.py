"""Local webhook receiver and fault-injecting delivery system.

Simulates Stripe webhook delivery to the agent under test with support for
signatures, duplicates, out-of-order arrival, delayed events, and dropped events.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from uuid import uuid4

from agentpay_harness.faults.actions import WebhookAction, WebhookActionType
from agentpay_harness.faults.engine import FaultEngine
from agentpay_harness.stripeio.recorder import EventRecorder, WebhookDelivery
from agentpay_harness.types import StripeEvent
from agentpay_harness.webhooks.signatures import (
    HARNESS_WEBHOOK_SECRET,
    generate_test_signature,
    verify_signature,
)

logger = logging.getLogger(__name__)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server to handle concurrent webhook requests without blocking."""

    daemon_threads = True
    allow_reuse_address = True


class WebhookReceiver:
    """Embedded HTTP server receiving webhooks from Stripe or test runners.

    Verifies Stripe signatures and forwards verified events to the agent callback.
    """

    def __init__(
        self,
        event_handler: Callable[[StripeEvent], None],
        recorder: EventRecorder | None = None,
        signing_secret: str = HARNESS_WEBHOOK_SECRET,
        verify_signatures: bool = True,
        raw_handler: Callable[[bytes, str], None] | None = None,
    ) -> None:
        self.event_handler = event_handler
        self.recorder = recorder
        self.signing_secret = signing_secret
        self.verify_signatures = verify_signatures
        #: When set, every POST body and its Stripe-Signature header are handed
        #: over untouched (no verification, no parsing) and answered with 200.
        self.raw_handler = raw_handler
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/webhook"

    def start(self, port: int = 0) -> int:
        """Start the receiver on the given port (0 for ephemeral port)."""
        receiver_self = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass  # suppress default logging

            def do_POST(self) -> None:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                sig_header = self.headers.get("Stripe-Signature", "")

                if receiver_self.raw_handler is not None:
                    receiver_self.raw_handler(body, sig_header)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"received": true}')
                    return

                if receiver_self.verify_signatures:
                    if not verify_signature(body, sig_header, receiver_self.signing_secret):
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b'{"error": "Invalid signature"}')
                        return

                try:
                    data = json.loads(body.decode("utf-8"))
                    event = StripeEvent(
                        event_id=data.get("id", f"evt_{uuid4().hex[:16]}"),
                        type=data.get("type", "unknown"),
                        data=data.get("data", {}),
                        created=int(data.get("created", int(time.time()))),
                        livemode=bool(data.get("livemode", False)),
                        pending_webhooks=int(data.get("pending_webhooks", 0)),
                        api_version=data.get("api_version"),
                    )

                    if receiver_self.recorder:
                        receiver_self.recorder.record_event(event)

                    receiver_self.event_handler(event)

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"received": true}')
                except Exception as exc:
                    logger.exception("Error processing webhook: %s", exc)
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Internal error"}')

        self._server = _ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._port

    def stop(self) -> None:
        """Stop the receiver HTTP server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def inject_event(self, event: StripeEvent, *, verify_signature_check: bool = True) -> None:
        """Directly deliver an event into the receiver without HTTP transport."""
        if self.recorder:
            self.recorder.record_event(event)
        self.event_handler(event)


class WebhookDeliverySystem:
    """Dispatches webhook events to the agent, applying fault engine chaos rules.

    Events are queued with :meth:`enqueue` while the agent pays, then delivered
    by :meth:`flush` the way Stripe would: asynchronously, signed, possibly
    duplicated, reordered (``reorder_window``), replayed later with the original
    (now stale) signature, or accompanied by forged events signed with the wrong
    secret.  Every delivery attempt is recorded with the harness's own
    signature verdict so invariant I8 can tell whether the agent processed a
    forged or stale event.
    """

    #: Secret used to sign forged events (deliberately not the harness secret).
    FORGED_SECRET = "whsec_test_attacker_controlled"  # noqa: S105 - test-only secret

    def __init__(
        self,
        target_callback: Callable[[StripeEvent], None],
        fault_engine: FaultEngine | None = None,
        recorder: EventRecorder | None = None,
        *,
        signing_secret: str = HARNESS_WEBHOOK_SECRET,
        reorder_window: int = 0,
        seed: int = 0,
        signature_tolerance_s: int = 300,
        clock_skews: Sequence[tuple[str | None, int]] = (),
    ) -> None:
        self.target_callback = target_callback
        self.fault_engine = fault_engine
        self.recorder = recorder
        self.signing_secret = signing_secret
        self.reorder_window = reorder_window
        self.signature_tolerance_s = signature_tolerance_s
        #: (event-type pattern or None for all, seconds) — the sender's clock is
        #: this far ahead (+) or behind (-) of the receiver's.
        self.clock_skews = list(clock_skews)
        self._rng = random.Random(seed)  # noqa: S311 - deterministic shuffling, not crypto
        self._lock = threading.Lock()
        self._queue: list[StripeEvent] = []

    def enqueue(self, event: StripeEvent) -> None:
        """Queue an event for delivery on the next :meth:`flush`."""
        with self._lock:
            self._queue.append(event)

    def deliver(self, event: StripeEvent) -> None:
        """Deliver a single event immediately (no cross-event reordering)."""
        self.enqueue(event)
        self.flush()

    def flush(self) -> None:
        """Deliver every queued event, applying webhook faults."""
        with self._lock:
            queued, self._queue = self._queue, []

        # (event, signature age in seconds, signing secret, fault description)
        in_order: list[tuple[StripeEvent, int, str, str]] = []
        deferred: list[tuple[StripeEvent, int, str, str]] = []

        for event in queued:
            actions = (
                self.fault_engine.intercept_webhook(event)
                if self.fault_engine is not None
                else [WebhookAction(WebhookActionType.DELIVER, event, 0, "Normal delivery")]
            )
            for action in actions:
                if action.action_type is WebhookActionType.DROP:
                    logger.debug("Dropping webhook event %s per fault rule", event.event_id)
                    continue
                if action.action_type is WebhookActionType.FORGE:
                    in_order.append((action.event, 0, self.FORGED_SECRET, action.fault_description))
                    continue
                if action.delay_ms > 0:
                    # A replay: same payload and original signature, delivered later.
                    deferred.append(
                        (
                            action.event,
                            action.delay_ms // 1000,
                            self.signing_secret,
                            action.fault_description,
                        )
                    )
                    continue
                in_order.append((action.event, 0, self.signing_secret, action.fault_description))

        if self.reorder_window > 1:
            in_order = self._reorder(in_order)

        for event, age_s, secret, description in in_order + deferred:
            self._send(event, age_s, secret, description)

    def _reorder(
        self, items: list[tuple[StripeEvent, int, str, str]]
    ) -> list[tuple[StripeEvent, int, str, str]]:
        """Seeded shuffle within windows; a window that comes out unchanged is reversed."""
        window = self.reorder_window
        out: list[tuple[StripeEvent, int, str, str]] = []
        for start in range(0, len(items), window):
            chunk = items[start : start + window]
            shuffled = list(chunk)
            self._rng.shuffle(shuffled)
            if len(chunk) > 1 and shuffled == chunk:
                shuffled.reverse()
            out.extend(
                (evt, age, secret, f"{desc}; reordered (window={window})")
                for evt, age, secret, desc in shuffled
            )
        return out

    def _send(self, event: StripeEvent, age_s: int, secret: str, description: str) -> None:
        payload = json.dumps(
            {
                "id": event.event_id,
                "object": "event",
                "type": event.type,
                "created": event.created,
                "livemode": event.livemode,
                "data": event.data,
            },
            sort_keys=True,
        ).encode("utf-8")
        skew_s = sum(
            s for pattern, s in self.clock_skews if pattern is None or fnmatch(event.type, pattern)
        )
        if skew_s:
            description = f"{description}; sender clock skewed {skew_s:+d}s"
        now = int(time.time())
        header = generate_test_signature(payload, secret, timestamp=now - age_s + skew_s)
        self._dispatch(event, payload, header, description)

    def send_signed(self, event: StripeEvent, secret: str, description: str) -> None:
        """Sign ``event`` with ``secret`` (e.g. a forger's) and deliver it now."""
        self._send(event, 0, secret, description)

    def deliver_genuine(self, event: StripeEvent, payload: bytes, signature_header: str) -> None:
        """Deliver a real Stripe event with its original body and signature, untouched."""
        self._dispatch(event, payload, signature_header, "Genuine Stripe delivery")

    def _dispatch(self, event: StripeEvent, payload: bytes, header: str, description: str) -> None:
        signature_valid = verify_signature(
            payload, header, self.signing_secret, tolerance=self.signature_tolerance_s
        )
        signed = replace(event, payload=payload, signature_header=header)

        if self.recorder is not None:
            self.recorder.record_delivery(
                WebhookDelivery(
                    event_id=event.event_id,
                    event_type=event.type,
                    signature_valid=signature_valid,
                    fault_description=description,
                    timestamp=datetime.now(tz=timezone.utc),
                )
            )
            self.recorder.record_event(signed)
        try:
            self.target_callback(signed)
        except Exception as exc:
            logger.warning("Agent on_event failed: %s", exc)
