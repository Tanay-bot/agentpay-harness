"""Event recorder — stores and merges RequestRecords and StripeEvents.

The recorder maintains a chronological timeline of all interactions with the
Stripe API during a test scenario and supports JSON serialization for replay.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agentpay_harness.types import RequestRecord, StripeEvent


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """A single entry in the merged request/event timeline."""

    timestamp: datetime
    entry_type: Literal["request", "event"]
    request: RequestRecord | None = None
    event: StripeEvent | None = None


class EventRecorder:
    """Thread-safe store for all :class:`RequestRecord` and :class:`StripeEvent` instances.

    Provides filtered queries and JSON round-trip serialization for test
    replay and post-mortem analysis.
    """

    def __init__(self) -> None:
        self._requests: list[RequestRecord] = []
        self._events: list[StripeEvent] = []
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
        entries: list[dict[str, object]] = json.loads(data)
        for item in entries:
            entry_type = item.get("entry_type")
            if entry_type == "request" and "request" in item:
                recorder.record_request(_deserialize_request(item["request"]))  # type: ignore[arg-type]
            elif entry_type == "event" and "event" in item:
                recorder.record_event(_deserialize_event(item["event"]))  # type: ignore[arg-type]
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


def _serialize_request(record: RequestRecord) -> dict[str, object]:
    """Convert a ``RequestRecord`` to a JSON-safe dict."""
    return {
        "id": record.id,
        "timestamp": record.timestamp.isoformat(),
        "method": record.method,
        "path": record.path,
        "request_body": record.request_body,
        "status_code": record.status_code,
        "response_body": record.response_body,
        "error": record.error,
        "response_time_ms": record.response_time_ms,
    }


def _deserialize_request(raw: dict[str, object]) -> RequestRecord:
    """Reconstruct a ``RequestRecord`` from a serialized dict."""
    ts = raw.get("timestamp")
    timestamp = datetime.fromisoformat(str(ts)) if ts else datetime.now(tz=UTC)
    return RequestRecord(
        id=str(raw.get("id", "")),
        timestamp=timestamp,
        method=str(raw.get("method", "")),
        path=str(raw.get("path", "")),
        request_body=raw.get("request_body") or {},  # type: ignore[arg-type]
        status_code=int(raw.get("status_code", 0)),  # type: ignore[arg-type]
        response_body=raw.get("response_body") or {},  # type: ignore[arg-type]
        error=str(raw["error"]) if raw.get("error") else None,
        response_time_ms=float(raw.get("response_time_ms", 0.0)),  # type: ignore[arg-type]
    )


def _serialize_event(event: StripeEvent) -> dict[str, object]:
    """Convert a ``StripeEvent`` to a JSON-safe dict."""
    return {
        "id": event.id,
        "timestamp": event.timestamp.isoformat(),
        "event_type": event.event_type,
        "data": event.data,
        "api_version": event.api_version,
        "livemode": event.livemode,
    }


def _deserialize_event(raw: dict[str, object]) -> StripeEvent:
    """Reconstruct a ``StripeEvent`` from a serialized dict."""
    ts = raw.get("timestamp")
    timestamp = datetime.fromisoformat(str(ts)) if ts else datetime.now(tz=UTC)
    return StripeEvent(
        id=str(raw.get("id", "")),
        timestamp=timestamp,
        event_type=str(raw.get("event_type", "")),
        data=raw.get("data") or {},  # type: ignore[arg-type]
        api_version=str(raw.get("api_version", "")) or None,
        livemode=bool(raw.get("livemode", False)),
    )
