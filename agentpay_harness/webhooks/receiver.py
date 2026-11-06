"""Local webhook receiver and fault-injecting delivery system.

Simulates Stripe webhook delivery to the agent under test with support for
signatures, duplicates, out-of-order arrival, delayed events, and dropped events.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from uuid import uuid4

from agentpay_harness.faults.actions import WebhookAction, WebhookActionType
from agentpay_harness.faults.engine import FaultEngine
from agentpay_harness.stripeio.recorder import EventRecorder
from agentpay_harness.types import StripeEvent
from agentpay_harness.webhooks.signatures import (
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
        signing_secret: str = "whsec_test_agentpay_local_secret_42",  # noqa: S107 - local test secret
        verify_signatures: bool = True,
    ) -> None:
        self.event_handler = event_handler
        self.recorder = recorder
        self.signing_secret = signing_secret
        self.verify_signatures = verify_signatures
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

    Injects duplicates, delay, drop, or out-of-order reordering deterministically.
    """

    def __init__(
        self,
        target_callback: Callable[[StripeEvent], None],
        fault_engine: FaultEngine | None = None,
        recorder: EventRecorder | None = None,
    ) -> None:
        self.target_callback = target_callback
        self.fault_engine = fault_engine
        self.recorder = recorder
        self._lock = threading.Lock()

    def deliver(self, event: StripeEvent) -> None:
        """Deliver an event, applying any configured webhook chaos rules."""
        if self.fault_engine is None:
            actions = [
                WebhookAction(
                    action_type=WebhookActionType.DELIVER,
                    event=event,
                    fault_description="Normal delivery — no fault engine",
                )
            ]
        else:
            actions = self.fault_engine.intercept_webhook(event)

        for action in actions:
            if action.action_type is WebhookActionType.DROP:
                logger.debug("Dropping webhook event %s per fault rule", event.event_id)
                continue

            if action.delay_ms > 0:
                time.sleep(action.delay_ms / 1000.0)

            if self.recorder is not None:
                self.recorder.record_event(action.event)

            try:
                self.target_callback(action.event)
            except Exception as exc:
                logger.warning("Agent on_event failed: %s", exc)
