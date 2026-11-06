"""Recording Stripe client wrapper that implements the StripeClient protocol.

Every API call is recorded as a RequestRecord, and an optional fault interceptor
can modify, drop, or duplicate requests before they hit Stripe (or the offline simulator).
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from agentpay_harness.faults.actions import FaultAction, FaultActionType
from agentpay_harness.safety import validate_no_live_mode, validate_stripe_key
from agentpay_harness.types import RequestRecord

logger = logging.getLogger(__name__)

# Optional stripe import — fall back to offline simulation if not installed
try:
    import stripe
    from stripe import StripeError

    HAS_STRIPE = True
except ImportError:
    stripe = None  # type: ignore[assignment]
    HAS_STRIPE = False

    class StripeError(Exception):  # type: ignore[no-redef]
        """Fallback StripeError for offline environments."""

        def __init__(
            self, message: str = "", http_status: int | None = None, code: str | None = None
        ) -> None:
            super().__init__(message)
            self.user_message = message
            self.http_status = http_status
            self.code = code


class _OfflineStripeSimulator:
    """In-memory simulation of Stripe test mode when running offline or without network.

    Accurately tracks PaymentIntent lifecycles, balance transactions, and idempotency key
    deduplication and mismatch errors.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payment_intents: dict[str, dict[str, Any]] = {}
        self._idempotency_store: dict[str, dict[str, Any]] = {}
        self._balance_transactions: list[dict[str, Any]] = []

    def create_payment_intent(
        self,
        amount: int,
        currency: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        capture_method: str = "automatic",
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            # Check idempotency
            if idempotency_key is not None:
                if idempotency_key in self._idempotency_store:
                    stored = self._idempotency_store[idempotency_key]
                    stored_params = stored["params"]
                    if (
                        stored_params.get("amount") != amount
                        or stored_params.get("currency") != currency
                    ):
                        # Idempotency key mismatch error (I5)
                        err_resp = {
                            "error": {
                                "type": "idempotency_error",
                                "code": "keys_with_different_params",
                                "message": (
                                    f"Keys for idempotent requests cannot be used with different "
                                    f"parameters. Original amount: {stored_params.get('amount')}, "
                                    f"new amount: {amount}"
                                ),
                            },
                            "_status_code": 400,
                        }
                        return 400, err_resp
                    # Return original response (I4)
                    return 200, copy.deepcopy(stored["response"])

            pi_id = f"pi_mock_{uuid4().hex[:20]}"
            status = "succeeded" if capture_method == "automatic" else "requires_capture"
            now_ts = int(time.time())

            response: dict[str, Any] = {
                "id": pi_id,
                "object": "payment_intent",
                "amount": amount,
                "amount_received": amount if status == "succeeded" else 0,
                "currency": currency.lower(),
                "status": status,
                "capture_method": capture_method,
                "created": now_ts,
                "livemode": False,
                "metadata": dict(metadata or {}),
                "client_secret": f"{pi_id}_secret_{uuid4().hex[:16]}",
                "_status_code": 200,
            }

            self._payment_intents[pi_id] = copy.deepcopy(response)

            if status == "succeeded":
                txn_id = f"txn_mock_{uuid4().hex[:20]}"
                txn = {
                    "id": txn_id,
                    "object": "balance_transaction",
                    "amount": amount,
                    "currency": currency.lower(),
                    "net": amount,
                    "type": "charge",
                    "source": pi_id,
                    "created": now_ts,
                    "status": "available",
                }
                self._balance_transactions.append(txn)

            if idempotency_key is not None:
                self._idempotency_store[idempotency_key] = {
                    "params": {"amount": amount, "currency": currency},
                    "response": copy.deepcopy(response),
                }

            return 200, copy.deepcopy(response)

    def capture_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: str | None = None,
        amount_to_capture: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return 404, {
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"No such payment_intent: '{payment_intent_id}'",
                    },
                    "_status_code": 404,
                }
            if pi.get("status") != "requires_capture":
                return 400, {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "payment_intent_unexpected_state",
                        "message": f"PaymentIntent is {pi.get('status')!r}, not requires_capture",
                    },
                    "_status_code": 400,
                }
            captured = pi["amount"] if amount_to_capture is None else amount_to_capture
            if not 0 < captured <= pi["amount"]:
                return 400, {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "amount_too_large",
                        "message": "amount_to_capture must be between 1 and the authorized amount",
                    },
                    "_status_code": 400,
                }

            pi["status"] = "succeeded"
            pi["amount_received"] = captured
            pi["amount_refunded"] = 0
            pi["_status_code"] = 200

            now_ts = int(time.time())
            txn_id = f"txn_mock_{uuid4().hex[:20]}"
            txn = {
                "id": txn_id,
                "object": "balance_transaction",
                "amount": pi["amount"],
                "currency": pi["currency"],
                "net": pi["amount"],
                "type": "charge",
                "source": payment_intent_id,
                "created": now_ts,
                "status": "available",
            }
            if captured != pi["amount"]:
                txn["amount"] = txn["net"] = captured
            self._balance_transactions.append(txn)
            return 200, copy.deepcopy(pi)

    def authorized_amount(self, payment_intent_id: str) -> int | None:
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            return None if pi is None else int(pi["amount"])

    def create_refund(
        self,
        payment_intent_id: str,
        *,
        amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Refund part or all of a captured PaymentIntent, as Stripe does.

        Rejects refunds larger than what is still refundable (captured minus
        already refunded) with ``amount_too_large``.
        """
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency_store:
                return 200, copy.deepcopy(self._idempotency_store[idempotency_key]["response"])
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return 404, {
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"No such payment_intent: '{payment_intent_id}'",
                    },
                    "_status_code": 404,
                }
            captured = int(pi.get("amount_received") or 0)
            already = int(pi.get("amount_refunded") or 0)
            refundable = captured - already
            requested = refundable if amount is None else amount
            if requested <= 0 or requested > refundable:
                return 400, {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "amount_too_large",
                        "message": (
                            f"Refund amount ({requested}) is greater than the unrefunded "
                            f"amount on the charge ({refundable})."
                        ),
                    },
                    "_status_code": 400,
                }
            pi["amount_refunded"] = already + requested
            refund = {
                "id": f"re_mock_{uuid4().hex[:20]}",
                "object": "refund",
                "amount": requested,
                "currency": pi["currency"],
                "payment_intent": payment_intent_id,
                "status": "succeeded",
                "created": int(time.time()),
                "_status_code": 200,
            }
            if idempotency_key is not None:
                self._idempotency_store[idempotency_key] = {
                    "params": {"amount": requested, "currency": pi["currency"]},
                    "response": copy.deepcopy(refund),
                }
            return 200, copy.deepcopy(refund)

    def cancel_payment_intent(self, payment_intent_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return 404, {
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"No such payment_intent: '{payment_intent_id}'",
                    },
                    "_status_code": 404,
                }
            pi["status"] = "canceled"
            pi["_status_code"] = 200
            return 200, copy.deepcopy(pi)

    def mark_processing(self, payment_intent_id: str) -> dict[str, Any] | None:
        """Turn a just-created PI into an async payment still ``processing``.

        Removes its balance transaction and rewrites the stored idempotent
        response, so retries see the same ``processing`` state.
        """
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return None
            pi["status"] = "processing"
            pi["amount_received"] = 0
            self._balance_transactions = [
                t for t in self._balance_transactions if t.get("source") != payment_intent_id
            ]
            for stored in self._idempotency_store.values():
                if stored["response"].get("id") == payment_intent_id:
                    stored["response"] = copy.deepcopy(pi)
            return copy.deepcopy(pi)

    def fail_async_payment(self, payment_intent_id: str) -> dict[str, Any] | None:
        """Settle a ``processing`` PI as failed (e.g. the bank debit bounced)."""
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return None
            pi["status"] = "requires_payment_method"
            pi["amount_received"] = 0
            pi["last_payment_error"] = {
                "type": "card_error",
                "code": "payment_failed",
                "message": "Asynchronous payment failed (fault injection)",
            }
            return copy.deepcopy(pi)

    def retrieve_payment_intent(self, payment_intent_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            pi = self._payment_intents.get(payment_intent_id)
            if pi is None:
                return 404, {
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"No such payment_intent: '{payment_intent_id}'",
                    },
                    "_status_code": 404,
                }
            return 200, copy.deepcopy(pi)

    def list_payment_intents(
        self, *, limit: int = 100, created: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(pi) for pi in list(self._payment_intents.values())[:limit]]

    def list_balance_transactions(
        self, *, limit: int = 100, created: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(txn) for txn in list(self._balance_transactions)[:limit]]


class RecordingStripeClient:
    """A Stripe client wrapper that records every request/response pair.

    Implements the StripeClient protocol. Supports real Stripe test mode,
    stripe-mock via base_url, or built-in offline simulation.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        fault_interceptor: Callable[[str, str, dict[str, Any] | None, int], FaultAction | None]
        | None = None,
        force_offline: bool = False,
    ) -> None:
        validate_stripe_key(api_key)
        self._api_key = api_key
        self._base_url = base_url
        self.fault_interceptor = fault_interceptor
        self._force_offline = (
            force_offline or not HAS_STRIPE or (base_url is None and "mock" in api_key)
        )

        self._records: list[RequestRecord] = []
        self._lock = threading.Lock()
        self._offline_sim = _OfflineStripeSimulator()
        self._async_failures: list[str] = []

        if HAS_STRIPE and not self._force_offline:
            self._stripe_kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url is not None:
                self._stripe_kwargs["api_base"] = base_url
        else:
            self._stripe_kwargs = {}

    # ------------------------------------------------------------------
    # StripeClient protocol methods
    # ------------------------------------------------------------------

    def create_payment_intent(
        self,
        amount: int,
        currency: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "metadata": metadata or {},
            **kwargs,
        }
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        def _do_offline() -> tuple[int, dict[str, Any]]:
            return self._offline_sim.create_payment_intent(
                amount=amount,
                currency=currency,
                idempotency_key=idempotency_key,
                metadata=metadata,
                capture_method=kwargs.get("capture_method", "automatic"),
            )

        def _do_stripe() -> Any:
            call_kwargs = dict(self._stripe_kwargs)
            if idempotency_key:
                call_kwargs["idempotency_key"] = idempotency_key
            return stripe.PaymentIntent.create(**params, **call_kwargs)

        return self._call(
            method="POST",
            path="/v1/payment_intents",
            body=params,
            headers=headers,
            idempotency_key=idempotency_key,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )

    def capture_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        path = f"/v1/payment_intents/{payment_intent_id}/capture"
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        def _do_offline() -> tuple[int, dict[str, Any]]:
            return self._offline_sim.capture_payment_intent(
                payment_intent_id,
                idempotency_key=idempotency_key,
                amount_to_capture=kwargs.get("amount_to_capture"),
            )

        def _do_stripe() -> Any:
            call_kwargs = dict(self._stripe_kwargs)
            if idempotency_key:
                call_kwargs["idempotency_key"] = idempotency_key
            return stripe.PaymentIntent.capture(payment_intent_id, **kwargs, **call_kwargs)

        return self._call(
            method="POST",
            path=path,
            body=kwargs or None,
            headers=headers,
            idempotency_key=idempotency_key,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )

    def cancel_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        path = f"/v1/payment_intents/{payment_intent_id}/cancel"

        def _do_offline() -> tuple[int, dict[str, Any]]:
            return self._offline_sim.cancel_payment_intent(payment_intent_id)

        def _do_stripe() -> Any:
            return stripe.PaymentIntent.cancel(payment_intent_id, **self._stripe_kwargs)

        return self._call(
            method="POST",
            path=path,
            body=None,
            headers={},
            idempotency_key=None,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )

    def create_refund(
        self,
        payment_intent_id: str,
        *,
        amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Refund a captured PaymentIntent (all of it when ``amount`` is None)."""
        params: dict[str, Any] = {"payment_intent": payment_intent_id}
        if amount is not None:
            params["amount"] = amount
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}

        def _do_offline() -> tuple[int, dict[str, Any]]:
            return self._offline_sim.create_refund(
                payment_intent_id, amount=amount, idempotency_key=idempotency_key
            )

        def _do_stripe() -> Any:
            call_kwargs = dict(self._stripe_kwargs)
            if idempotency_key:
                call_kwargs["idempotency_key"] = idempotency_key
            return stripe.Refund.create(**params, **call_kwargs)

        return self._call(
            method="POST",
            path="/v1/refunds",
            body=params,
            headers=headers,
            idempotency_key=idempotency_key,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )

    def retrieve_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        path = f"/v1/payment_intents/{payment_intent_id}"

        def _do_offline() -> tuple[int, dict[str, Any]]:
            return self._offline_sim.retrieve_payment_intent(payment_intent_id)

        def _do_stripe() -> Any:
            return stripe.PaymentIntent.retrieve(payment_intent_id, **self._stripe_kwargs)

        return self._call(
            method="GET",
            path=path,
            body=None,
            headers={},
            idempotency_key=None,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )

    def list_payment_intents(
        self, *, limit: int = 100, created: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        path = "/v1/payment_intents"
        params: dict[str, Any] = {"limit": limit}
        if created:
            params["created"] = created

        def _do_offline() -> tuple[int, dict[str, Any]]:
            data = self._offline_sim.list_payment_intents(limit=limit, created=created)
            return 200, {"object": "list", "data": data, "has_more": False, "_status_code": 200}

        def _do_stripe() -> Any:
            return stripe.PaymentIntent.list(**params, **self._stripe_kwargs)

        res = self._call(
            method="GET",
            path=path,
            body=params,
            headers={},
            idempotency_key=None,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )
        if isinstance(res, dict) and "data" in res:
            return cast("list[dict[str, Any]]", res["data"])
        return []

    def list_balance_transactions(
        self, *, limit: int = 100, created: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        path = "/v1/balance_transactions"
        params: dict[str, Any] = {"limit": limit}
        if created:
            params["created"] = created

        def _do_offline() -> tuple[int, dict[str, Any]]:
            data = self._offline_sim.list_balance_transactions(limit=limit, created=created)
            return 200, {"object": "list", "data": data, "has_more": False, "_status_code": 200}

        def _do_stripe() -> Any:
            return stripe.BalanceTransaction.list(**params, **self._stripe_kwargs)

        res = self._call(
            method="GET",
            path=path,
            body=params,
            headers={},
            idempotency_key=None,
            offline_fn=_do_offline,
            stripe_fn=_do_stripe,
        )
        if isinstance(res, dict) and "data" in res:
            return cast("list[dict[str, Any]]", res["data"])
        return []

    # ------------------------------------------------------------------
    # Record access
    # ------------------------------------------------------------------

    def get_records(self) -> list[RequestRecord]:
        """Return a snapshot of all recorded requests."""
        with self._lock:
            return list(self._records)

    def take_async_failures(self) -> list[str]:
        """Return (and clear) PI ids left ``processing`` by an async-failure fault."""
        with self._lock:
            pending, self._async_failures = self._async_failures, []
        return pending

    def settle_async_failure(self, payment_intent_id: str) -> dict[str, Any] | None:
        """Fail a ``processing`` PI on the offline backend; returns its new state."""
        return self._offline_sim.fail_async_payment(payment_intent_id)

    # ------------------------------------------------------------------
    # Dispatch & Fault Interception
    # ------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        idempotency_key: str | None,
        offline_fn: Callable[[], tuple[int, dict[str, Any]]],
        stripe_fn: Callable[[], Any],
        attempt: int = 1,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()

        # Check fault injection
        fault: FaultAction | None = None
        if self.fault_interceptor is not None:
            fault = self.fault_interceptor(method, path, body, attempt)

        if fault is not None:
            if fault.action_type is FaultActionType.DROP_RESPONSE:
                # Backend processes the request, but response is dropped on the wire (timeout)
                server_id = None
                server_meta: dict[str, Any] = {}
                try:
                    if self._force_offline or not HAS_STRIPE or stripe is None:
                        _, s_resp = offline_fn()
                        server_id = s_resp.get("id")
                        server_meta = s_resp.get("metadata", {})
                    else:
                        s_raw = stripe_fn()
                        server_id = getattr(s_raw, "id", None) or (
                            s_raw.get("id") if isinstance(s_raw, dict) else None
                        )
                except Exception as exc:
                    # Best effort: the dropped response is still recorded below.
                    logger.debug(
                        "Could not resolve server-side object for dropped response: %s", exc
                    )

                recorded_body = {
                    "error": {
                        "type": "api_connection_error",
                        "message": "Request timed out (fault injection)",
                    },
                    "_status_code": 0,
                }
                if server_id:
                    recorded_body["id"] = server_id
                    recorded_body["metadata"] = server_meta

                record = RequestRecord(
                    method=method,
                    path=path,
                    body=body,
                    headers=headers,
                    idempotency_key=idempotency_key,
                    timestamp=datetime.now(tz=timezone.utc),
                    response_status=0,
                    response_body=recorded_body,
                    response_time_ms=0.0,
                    fault_injected=fault.fault_description or "DROP_RESPONSE",
                )
                self._append_record(record)
                return {
                    "error": {
                        "type": "api_connection_error",
                        "message": "Request timed out (fault injection)",
                    },
                    "_status_code": 0,
                }

            if fault.action_type is FaultActionType.INJECT_429:
                resp_body = fault.response_override or {
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Too many requests (fault injection)",
                        "code": "rate_limit",
                    },
                    "_status_code": 429,
                    "_retry_after": 0.05,
                }
                if "_status_code" not in resp_body:
                    resp_body["_status_code"] = 429
                record = RequestRecord(
                    method=method,
                    path=path,
                    body=body,
                    headers=headers,
                    idempotency_key=idempotency_key,
                    timestamp=datetime.now(tz=timezone.utc),
                    response_status=429,
                    response_body=resp_body,
                    response_time_ms=0.0,
                    fault_injected=fault.fault_description or "INJECT_429",
                )
                self._append_record(record)
                return resp_body

            if fault.action_type is FaultActionType.INJECT_5XX:
                status = 500
                if fault.response_override and "status" in fault.response_override:
                    status = int(fault.response_override["status"])
                resp_body = fault.response_override or {
                    "error": {
                        "type": "api_error",
                        "message": "Internal server error (fault injection)",
                    },
                    "_status_code": status,
                }
                if "_status_code" not in resp_body:
                    resp_body["_status_code"] = status
                record = RequestRecord(
                    method=method,
                    path=path,
                    body=body,
                    headers=headers,
                    idempotency_key=idempotency_key,
                    timestamp=datetime.now(tz=timezone.utc),
                    response_status=status,
                    response_body=resp_body,
                    response_time_ms=0.0,
                    fault_injected=fault.fault_description or f"INJECT_{status}",
                )
                self._append_record(record)
                return resp_body

            if fault.action_type is FaultActionType.DELAY and fault.delay_ms:
                time.sleep(fault.delay_ms / 1000.0)

            if fault.action_type is FaultActionType.PARTIAL_CAPTURE:
                offline = self._force_offline or not HAS_STRIPE or stripe is None
                parts = path.strip("/").split("/")
                pi_id = parts[2] if len(parts) == 4 and parts[3] == "capture" else None
                authorized = self._offline_sim.authorized_amount(pi_id) if pi_id else None
                if not offline or pi_id is None or authorized is None:
                    logger.warning("partial_capture only applies offline to a capture; ignoring")
                else:
                    percent = int((fault.response_override or {}).get("percent", 50))
                    partial = max(1, authorized * percent // 100)

                    def _partial_offline() -> tuple[int, dict[str, Any]]:
                        return self._offline_sim.capture_payment_intent(
                            str(pi_id), idempotency_key=idempotency_key, amount_to_capture=partial
                        )

                    resp = self._call_internal(
                        method,
                        path,
                        body,
                        headers,
                        idempotency_key,
                        _partial_offline,
                        stripe_fn,
                        t0,
                    )
                    with self._lock:
                        if self._records and self._records[-1].response_body is resp:
                            self._records[-1] = replace(
                                self._records[-1], fault_injected=fault.fault_description
                            )
                    return resp

            if fault.action_type is FaultActionType.ASYNC_FAILURE:
                resp = self._call_internal(
                    method, path, body, headers, idempotency_key, offline_fn, stripe_fn, t0
                )
                pi_id = resp.get("id")
                offline = self._force_offline or not HAS_STRIPE or stripe is None
                if not offline:
                    logger.warning("async_payment_failure is only supported offline; ignoring")
                    return resp
                if pi_id and resp.get("object") == "payment_intent":
                    processing = self._offline_sim.mark_processing(str(pi_id))
                    if processing is not None:
                        # The record holds this same dict, so it reflects the change too.
                        resp.clear()
                        resp.update(processing)
                        resp["_status_code"] = 200
                        with self._lock:
                            self._async_failures.append(str(pi_id))
                return resp

            if fault.action_type is FaultActionType.DUPLICATE:
                first = self._call_internal(
                    method, path, body, headers, idempotency_key, offline_fn, stripe_fn, t0
                )
                second = self._call_internal(
                    method, path, body, headers, idempotency_key, offline_fn, stripe_fn, t0
                )
                _ = second
                return first

        return self._call_internal(
            method, path, body, headers, idempotency_key, offline_fn, stripe_fn, t0
        )

    def _call_internal(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        idempotency_key: str | None,
        offline_fn: Callable[[], tuple[int, dict[str, Any]]],
        stripe_fn: Callable[[], Any],
        t0: float,
    ) -> dict[str, Any]:
        if self._force_offline or not HAS_STRIPE or stripe is None:
            status_code, resp_dict = offline_fn()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            validate_no_live_mode(resp_dict)
            record = RequestRecord(
                method=method,
                path=path,
                body=body,
                headers=headers,
                idempotency_key=idempotency_key,
                timestamp=datetime.now(tz=timezone.utc),
                response_status=status_code,
                response_body=resp_dict,
                response_time_ms=elapsed_ms,
                fault_injected=None,
            )
            self._append_record(record)
            return resp_dict

        # Online / real Stripe / stripe-mock execution
        try:
            raw = stripe_fn()
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if hasattr(raw, "to_dict_recursive"):
                resp_dict = raw.to_dict_recursive()
            elif hasattr(raw, "to_dict"):
                resp_dict = dict(raw)
            else:
                resp_dict = dict(raw)

            resp_dict["_status_code"] = 200
            validate_no_live_mode(resp_dict)

            record = RequestRecord(
                method=method,
                path=path,
                body=body,
                headers=headers,
                idempotency_key=idempotency_key,
                timestamp=datetime.now(tz=timezone.utc),
                response_status=200,
                response_body=resp_dict,
                response_time_ms=elapsed_ms,
                fault_injected=None,
            )
            self._append_record(record)
            return resp_dict

        except StripeError as exc:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            status_code = getattr(exc, "http_status", 400) or 400
            err_dict: dict[str, Any] = {
                "error": {
                    "type": getattr(exc, "code", "stripe_error"),
                    "message": str(getattr(exc, "user_message", exc) or exc),
                },
                "_status_code": status_code,
            }
            record = RequestRecord(
                method=method,
                path=path,
                body=body,
                headers=headers,
                idempotency_key=idempotency_key,
                timestamp=datetime.now(tz=timezone.utc),
                response_status=status_code,
                response_body=err_dict,
                response_time_ms=elapsed_ms,
                fault_injected=None,
            )
            self._append_record(record)
            return err_dict

    def _append_record(self, record: RequestRecord) -> None:
        with self._lock:
            self._records.append(record)
