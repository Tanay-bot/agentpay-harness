"""Fault injection engine — deterministic, seed-based fault scheduling.

The FaultEngine evaluates a list of FaultConfig rules against each outgoing
API request and incoming webhook event, producing FaultAction or WebhookAction
objects that describe the fault to apply.

All randomness is driven by random.Random(seed) so scenarios are fully reproducible.
"""

from __future__ import annotations

import fnmatch
import random
import threading
from typing import Any

from agentpay_harness.faults.actions import (
    PASS_THROUGH,
    FaultAction,
    FaultActionType,
    WebhookAction,
    WebhookActionType,
)
from agentpay_harness.faults.transport import (
    delay_response,
    drop_response_action,
    duplicate_request,
    inject_5xx,
    inject_429,
    match_request,
)
from agentpay_harness.faults.webhook import (
    duplicate_webhook,
)
from agentpay_harness.types import FaultConfig, FaultType, StripeEvent


class FaultEngine:
    """Evaluates configured fault rules against requests and webhook events."""

    def __init__(self, fault_configs: list[FaultConfig], seed: int = 0) -> None:
        self._configs = list(fault_configs)
        self._rng = random.Random(seed)  # noqa: S311 - seeded for deterministic faults, not crypto
        self._lock = threading.Lock()
        self._match_counts: dict[str, int] = {}
        self._occurrence_counts: dict[str, int] = {}

    def intercept_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        attempt: int,
    ) -> FaultAction:
        """Evaluate fault rules against an outgoing API request."""
        with self._lock:
            for cfg in self._configs:
                if not self._matches_request(cfg, method, path):
                    continue

                rule_id = self._rule_id(cfg)
                self._match_counts[rule_id] = self._match_counts.get(rule_id, 0) + 1
                match_num = self._match_counts[rule_id]

                # Specific occurrence check (e.g. occurrence: 1 -> only fire on 1st match)
                if cfg.occurrence is not None and match_num != cfg.occurrence:
                    continue

                # Max times limit check
                fired = self._occurrence_counts.get(rule_id, 0)
                if cfg.times is not None and fired >= cfg.times:
                    continue

                self._occurrence_counts[rule_id] = fired + 1
                return self._make_request_action(cfg)

        return PASS_THROUGH

    def intercept_webhook(self, event: StripeEvent) -> list[WebhookAction]:
        """Evaluate fault rules against an incoming webhook event."""
        actions: list[WebhookAction] = []

        with self._lock:
            for cfg in self._configs:
                if not self._matches_event(cfg, event):
                    continue

                rule_id = self._rule_id(cfg)
                self._match_counts[rule_id] = self._match_counts.get(rule_id, 0) + 1
                match_num = self._match_counts[rule_id]

                if cfg.occurrence is not None and match_num != cfg.occurrence:
                    continue

                fired = self._occurrence_counts.get(rule_id, 0)
                if cfg.times is not None and fired >= cfg.times:
                    continue

                self._occurrence_counts[rule_id] = fired + 1
                actions.extend(self._make_webhook_actions(cfg, event))

        if not actions:
            actions.append(
                WebhookAction(
                    action_type=WebhookActionType.DELIVER,
                    event=event,
                    fault_description="Normal delivery — no fault",
                )
            )

        return actions

    @staticmethod
    def _matches_request(cfg: FaultConfig, method: str, path: str) -> bool:
        on_pattern = getattr(cfg, "on", None) or getattr(cfg, "target", None)
        if on_pattern is None:
            return False
        return match_request(str(on_pattern), method, path)

    @staticmethod
    def _matches_event(cfg: FaultConfig, event: StripeEvent) -> bool:
        event_pattern = getattr(cfg, "event", None) or getattr(cfg, "target", None)
        if event_pattern is None:
            return False
        pattern_str = str(event_pattern)
        if "*" in pattern_str:
            return fnmatch.fnmatch(event.event_type, pattern_str)
        return event.event_type == pattern_str

    @staticmethod
    def _rule_id(cfg: FaultConfig) -> str:
        f_type = getattr(cfg, "type", None) or getattr(cfg, "fault_type", None)
        type_val = f_type.value if hasattr(f_type, "value") else str(f_type)
        target = cfg.on or cfg.event or ""
        return f"{type_val}:{target}"

    def _make_request_action(self, cfg: FaultConfig) -> FaultAction:
        ft = getattr(cfg, "type", None) or getattr(cfg, "fault_type", None)

        if ft in (
            FaultType.DROP_RESPONSE,
            FaultType.TIMEOUT if hasattr(FaultType, "TIMEOUT") else None,
        ):
            return drop_response_action()

        if ft is FaultType.RATE_LIMIT:
            return inject_429()

        if ft is FaultType.SERVER_ERROR:
            return inject_5xx(self._rng)

        if ft is FaultType.DELAY_RESPONSE:
            delay_ms = cfg.ms or 1000
            return delay_response(delay_ms)

        if ft is FaultType.DUPLICATE_REQUEST:
            return duplicate_request()

        return PASS_THROUGH

    def _make_webhook_actions(self, cfg: FaultConfig, event: StripeEvent) -> list[WebhookAction]:
        ft = getattr(cfg, "type", None) or getattr(cfg, "fault_type", None)

        if ft is FaultType.DUPLICATE_WEBHOOK:
            times = cfg.times or 2
            return duplicate_webhook(event, times)

        if ft is FaultType.REORDER_WEBHOOKS:
            window = cfg.window or 3
            return [
                WebhookAction(
                    action_type=WebhookActionType.REORDER,
                    event=event,
                    delay_ms=20,
                    fault_description=f"Reorder webhook window={window}",
                )
            ]

        if ft is FaultType.REPLAY_WEBHOOK:
            delay_ms = (cfg.delay_s or 5) * 1000
            return [
                WebhookAction(
                    action_type=WebhookActionType.DELIVER,
                    event=event,
                    delay_ms=0,
                    fault_description="Original delivery",
                ),
                WebhookAction(
                    action_type=WebhookActionType.DUPLICATE,
                    event=event,
                    delay_ms=delay_ms,
                    fault_description=f"Replayed webhook delayed {cfg.delay_s}s",
                ),
            ]

        return [
            WebhookAction(
                action_type=WebhookActionType.DELIVER,
                event=event,
                fault_description="Normal delivery",
            )
        ]
