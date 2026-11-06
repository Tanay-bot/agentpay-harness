"""Event recorder — stores and merges RequestRecords and StripeEvents.

The recorder maintains a chronological timeline of all interactions with the
Stripe API during a test scenario and supports JSON serialization for replay.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from agentpay_harness.types import RequestRecord, StripeEvent


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """A single entry in the merged request/event timeline."""

    timestamp: datetime
    entry_type: Literal["request", "event"]
    request: RequestRecord | None = None
    event: StripeEvent | None = None


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """One webhook delivery attempt made by the harness to the agent.

    ``signature_valid`` is the harness's own verdict on the delivered signature
    (wrong secret, or timestamp outside the tolerance window, means False).
    """

    event_id: str
    event_type: str
    signature_valid: bool
    fault_description: str
    timestamp: datetime


class EventRecorder:
    """Thread-safe store for all :class:`RequestRecord` and :class:`StripeEvent` instances.

    Provides filtered queries and JSON round-trip serialization for test
    replay and post-mortem analysis.
    """

    def __init__(self) -> None:
        self._requests: list[RequestRecord] = []
        self._events: list[StripeEvent] = []
        self._deliveries: list[WebhookDelivery] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def requests(self) -> list[RequestRecord]:
        """Return a copy of all recorded request records."""
        with self._lock:
            return list(self._requests)

    @property
    def events(self) -> list[StripeEvent]:
        """Return a copy of all recorded webhook events."""
        with self._lock:
            return list(self._events)

    @property
    def deliveries(self) -> list[WebhookDelivery]:
        """Return a copy of all webhook delivery attempts."""
        with self._lock:
            return list(self._deliveries)

    @property
    def timeline(self) -> list[TimelineEntry]:
        """Return the merged chronological timeline."""
        return self.get_timeline()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_request(self, record: RequestRecord) -> None:
        """Append a request record to the log."""
        with self._lock:
            self._requests.append(record)

    def record_delivery(self, delivery: WebhookDelivery) -> None:
        """Append a webhook delivery attempt (with its signature verdict)."""
        with self._lock:
            self._deliveries.append(delivery)

    def record_event(self, event: StripeEvent) -> None:
        """Append a webhook event to the log."""
        with self._lock:
            self._events.append(event)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_timeline(self) -> list[TimelineEntry]:
        """Return a merged, chronologically sorted timeline of requests and events."""
        with self._lock:
            entries: list[TimelineEntry] = []
            for req in self._requests:
                entries.append(
                    TimelineEntry(
                        timestamp=req.timestamp,
                        entry_type="request",
                        request=req,
                    )
                )
            for evt in self._events:
                entries.append(
                    TimelineEntry(
                        timestamp=evt.timestamp,
                        entry_type="event",
                        event=evt,
                    )
                )
        entries.sort(key=lambda e: e.timestamp)
        return entries

    def get_requests(
        self,
        method: str | None = None,
        path_pattern: str | None = None,
    ) -> list[RequestRecord]:
        """Return matching request records.

        Parameters
        ----------
        method:
            HTTP method filter (e.g. ``"POST"``).  Case-insensitive.
        path_pattern:
            Regex pattern matched against the request path.
        """
        with self._lock:
            result = list(self._requests)

        if method is not None:
            upper = method.upper()
            result = [r for r in result if r.method.upper() == upper]
        if path_pattern is not None:
            compiled = re.compile(path_pattern)
            result = [r for r in result if compiled.search(r.path)]
        return result

    def get_events(self, event_type: str | None = None) -> list[StripeEvent]:
        """Return matching webhook events, optionally filtered by type."""
        with self._lock:
            result = list(self._events)

        if event_type is not None:
            result = [e for e in result if e.event_type == event_type]
        return result

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialize the full timeline to a JSON string."""
        timeline = self.get_timeline()
        payload: list[dict[str, object]] = []
        for entry in timeline:
            item: dict[str, object] = {
                "timestamp": entry.timestamp.isoformat(),
                "entry_type": entry.entry_type,
            }
            if entry.request is not None:
                item["request"] = _serialize_request(entry.request)
            if entry.event is not None:
                item["event"] = _serialize_event(entry.event)
            payload.append(item)
        return json.dumps(payload, indent=2, default=str)

    @classmethod
    def from_json(cls, data: str) -> EventRecorder:
        """Deserialize a JSON timeline into a new :class:`EventRecorder`."""
        recorder = cls()
        entries: list[dict[str, Any]] = json.loads(data)
        for item in entries:
            entry_type = item.get("entry_type")
            if entry_type == "request" and "request" in item:
                recorder.record_request(_deserialize_request(item["request"]))
            elif entry_type == "event" and "event" in item:
                recorder.record_event(_deserialize_event(item["event"]))
        return recorder

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._requests) + len(self._events)


# ======================================================================
# Private serialization helpers
# ======================================================================


def _serialize_request(record: RequestRecord) -> dict[str, Any]:
    """Convert a ``RequestRecord`` to a JSON-safe dict."""
    return {
        "record_id": record.record_id,
        "timestamp": record.timestamp.isoformat(),
        "method": record.method,
        "path": record.path,
        "body": record.body,
        "headers": record.headers,
        "idempotency_key": record.idempotency_key,
        "response_status": record.response_status,
        "response_body": record.response_body,
        "response_time_ms": record.response_time_ms,
        "fault_injected": record.fault_injected,
    }


def _deserialize_request(raw: dict[str, Any]) -> RequestRecord:
    """Reconstruct a ``RequestRecord`` from a serialized dict."""
    ts = raw.get("timestamp")
    return RequestRecord(
        record_id=str(raw.get("record_id", "")),
        timestamp=datetime.fromisoformat(str(ts)) if ts else datetime.now(tz=timezone.utc),
        method=str(raw.get("method", "")),
        path=str(raw.get("path", "")),
        body=raw.get("body"),
        headers=dict(raw.get("headers") or {}),
        idempotency_key=raw.get("idempotency_key"),
        response_status=raw.get("response_status"),
        response_body=raw.get("response_body"),
        response_time_ms=raw.get("response_time_ms"),
        fault_injected=raw.get("fault_injected"),
    )


def _serialize_event(event: StripeEvent) -> dict[str, Any]:
    """Convert a ``StripeEvent`` to a JSON-safe dict (payload/signature are not kept)."""
    return {
        "event_id": event.event_id,
        "type": event.type,
        "created": event.created,
        "data": event.data,
        "api_version": event.api_version,
        "livemode": event.livemode,
    }


def _deserialize_event(raw: dict[str, Any]) -> StripeEvent:
    """Reconstruct a ``StripeEvent`` from a serialized dict."""
    return StripeEvent(
        event_id=str(raw.get("event_id", "")),
        type=str(raw.get("type", "")),
        data=dict(raw.get("data") or {}),
        created=int(raw.get("created", 0)),
        api_version=raw.get("api_version"),
        livemode=bool(raw.get("livemode", False)),
    )
