"""Scenario loader and validator for agentpay-harness.

Loads YAML or JSON scenario definition files, validates invariant IDs and fault
configurations, and produces typed :class:`ScenarioConfig` objects.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agentpay_harness.types import (
    FaultConfig,
    FaultType,
    ScenarioConfig,
    UnsupportedCurrencyError,
    require_usd,
)

# Optional PyYAML support with built-in lightweight fallback
try:
    import yaml

    HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

VALID_INVARIANTS = {f"I{n}" for n in range(1, 14)}


class ScenarioValidationError(ValueError):
    """A scenario file is malformed.  The message names the offending field."""


FAULT_TYPE_MAP: dict[str, FaultType] = {
    "drop_response": FaultType.DROP_RESPONSE,
    "drop": FaultType.DROP_RESPONSE,
    "duplicate_request": FaultType.DUPLICATE_REQUEST,
    "duplicate": FaultType.DUPLICATE_REQUEST,
    "duplicate_webhook": FaultType.DUPLICATE_WEBHOOK,
    "reorder_webhooks": FaultType.REORDER_WEBHOOKS,
    "reorder": FaultType.REORDER_WEBHOOKS,
    "replay_webhook": FaultType.REPLAY_WEBHOOK,
    "replay": FaultType.REPLAY_WEBHOOK,
    "rate_limit": FaultType.RATE_LIMIT,
    "server_error": FaultType.SERVER_ERROR,
    "delay_response": FaultType.DELAY_RESPONSE,
    "delay": FaultType.DELAY_RESPONSE,
    "clock_skew": FaultType.CLOCK_SKEW,
    "forged_webhook": FaultType.FORGED_WEBHOOK,
    "async_payment_failure": FaultType.ASYNC_PAYMENT_FAILURE,
    "partial_capture": FaultType.PARTIAL_CAPTURE,
    "interleave": FaultType.INTERLEAVE,
}

#: Faults that act on API requests and therefore need an ``on: METHOD /path``.
REQUEST_FAULTS = {
    FaultType.DROP_RESPONSE,
    FaultType.DUPLICATE_REQUEST,
    FaultType.RATE_LIMIT,
    FaultType.SERVER_ERROR,
    FaultType.DELAY_RESPONSE,
    FaultType.ASYNC_PAYMENT_FAILURE,
    FaultType.PARTIAL_CAPTURE,
}
#: Faults that act on one webhook event type and therefore need ``event:``.
EVENT_FAULTS = {
    FaultType.DUPLICATE_WEBHOOK,
    FaultType.REPLAY_WEBHOOK,
    FaultType.FORGED_WEBHOOK,
}

_TOP_LEVEL_KEYS = {
    "name",
    "description",
    "budget_usd",
    "task",
    "seed",
    "concurrency",
    "assert",
    "assertions",
    "faults",
    "capture_method",
    "abort",
    "settle",
    "refund",
    "currency",
    "agents",
    "webhook_source",
    "cli_triggers",
    "cli_timeout_s",
}
_FAULT_KEYS = {
    "type",
    "on",
    "event",
    "occurrence",
    "times",
    "ms",
    "window",
    "delay_s",
    "percent",
    "skew_s",
    "schedule",
}
_HTTP_METHODS = {"GET", "POST", "DELETE"}


def _simple_yaml_parse(text: str) -> dict[str, Any]:
    """Lightweight YAML parser for scenario files without external dependencies."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    current_dict_item: dict[str, Any] | None = None

    lines = text.splitlines()
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        # Top-level key: value or start of list
        if indent == 0 and ":" in line:
            if current_dict_item is not None and current_list is not None:
                current_list.append(current_dict_item)
                current_dict_item = None
            if current_list is not None and current_key:
                result[current_key] = current_list
                current_list = None

            k, _, v = line.partition(":")
            current_key = k.strip()
            v_str = v.strip()
            if v_str == "":
                current_list = []
                result[current_key] = current_list
            else:
                current_list = None
                result[current_key] = _parse_val(v_str)

        # List item
        elif stripped.startswith("-"):
            if current_list is None:
                current_list = []
                if current_key:
                    result[current_key] = current_list

            item_str = stripped[1:].strip()
            if ":" in item_str:
                # Start of a dict list item (e.g. faults list)
                if current_dict_item is not None:
                    current_list.append(current_dict_item)
                current_dict_item = {}
                sub_k, _, sub_v = item_str.partition(":")
                current_dict_item[sub_k.strip()] = _parse_val(sub_v.strip())
            else:
                # Scalar list item (e.g. assert list)
                if current_dict_item is not None:
                    current_list.append(current_dict_item)
                    current_dict_item = None
                current_list.append(_parse_val(item_str))

        # Nested dict key in list item
        elif indent >= 4 and ":" in line and current_dict_item is not None:
            sub_k, _, sub_v = stripped.partition(":")
            current_dict_item[sub_k.strip()] = _parse_val(sub_v.strip())

    if current_dict_item is not None and current_list is not None:
        current_list.append(current_dict_item)

    return result


def _parse_val(v: str) -> Any:
    """Parse scalar yaml values."""
    if "#" in v:
        # Strip inline comments if not quoted
        if not (v.startswith('"') or v.startswith("'")):
            v = v.split("#")[0].strip()

    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        items = [x.strip() for x in inner.split(",") if x.strip()]
        return [_parse_val(x) for x in items]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() == "null" or v == "":
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def _fail(field_path: str, problem: str) -> ScenarioValidationError:
    return ScenarioValidationError(f"{field_path}: {problem}")


def _parse_budget(value: Any) -> Decimal:
    if value is None:
        raise _fail("budget_usd", "required field is missing")
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise _fail("budget_usd", f"must be a number like 200 or 49.99, got {value!r}")
    try:
        budget = Decimal(str(value).strip())
    except InvalidOperation:
        raise _fail("budget_usd", f"must be a number like 200 or 49.99, got {value!r}") from None
    if not budget.is_finite():
        raise _fail("budget_usd", f"must be a finite number, got {value!r}")
    if budget <= 0:
        raise _fail("budget_usd", f"must be greater than 0, got {value!r}")
    if budget.as_tuple().exponent < -2:  # type: ignore[operator]
        raise _fail("budget_usd", f"must have at most 2 decimal places (cents), got {value!r}")
    return budget


def _parse_int(value: Any, field_path: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(field_path, f"must be an integer, got {value!r}")
    if value < minimum:
        raise _fail(field_path, f"must be >= {minimum}, got {value}")
    return value


def _opt_int(f: dict[str, Any], key: str, path: str, *, minimum: int = 1) -> int | None:
    if f.get(key) is None:
        return None
    return _parse_int(f[key], f"{path}.{key}", minimum=minimum)


def _parse_bool(value: Any, field_path: str = "value") -> bool:
    """Parse a YAML boolean that may arrive as a string from the fallback parser."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in (
        "true",
        "yes",
        "on",
        "false",
        "no",
        "off",
    ):
        return value.strip().lower() in ("true", "yes", "on")
    raise _fail(field_path, f"must be true or false, got {value!r}")


def _parse_fault(f: Any, index: int, agents: int) -> FaultConfig:
    path = f"faults[{index}]"
    if isinstance(f, FaultConfig):
        return f
    if not isinstance(f, dict):
        raise _fail(path, f"must be a mapping with a 'type', got {f!r}")
    # YAML 1.1 reads an unquoted `on:` key as the boolean True; map it back.
    if True in f:
        f = {("on" if k is True else k): v for k, v in f.items()}

    unknown = sorted(set(f) - _FAULT_KEYS)
    if unknown:
        raise _fail(path, f"unknown field(s) {unknown}. Allowed: {sorted(_FAULT_KEYS)}")

    raw_type = f.get("type")
    if not raw_type:
        raise _fail(f"{path}.type", "required field is missing")
    type_str = str(raw_type).strip().lower()
    if type_str not in FAULT_TYPE_MAP:
        raise _fail(
            f"{path}.type",
            f"unknown fault type {raw_type!r}. Valid types: {sorted(FAULT_TYPE_MAP)}",
        )
    fault_type = FAULT_TYPE_MAP[type_str]

    on = f.get("on")
    if fault_type is FaultType.PARTIAL_CAPTURE and on is None:
        on = "POST /v1/payment_intents/*/capture"
    if fault_type in REQUEST_FAULTS:
        if not on:
            raise _fail(f"{path}.on", f"'{type_str}' needs 'on: METHOD /path'")
        method, _, req_path = str(on).strip().partition(" ")
        if method.upper() not in _HTTP_METHODS or not req_path.startswith("/"):
            raise _fail(f"{path}.on", f"must look like 'POST /v1/payment_intents', got {on!r}")
    elif on is not None:
        raise _fail(f"{path}.on", f"'{type_str}' does not act on requests; remove 'on'")

    event = f.get("event")
    if fault_type in EVENT_FAULTS and not event:
        raise _fail(f"{path}.event", f"'{type_str}' needs 'event:' (e.g. payment_intent.succeeded)")

    percent = _opt_int(f, "percent", path)
    if fault_type is FaultType.PARTIAL_CAPTURE:
        if percent is None or not 1 <= percent <= 99:
            raise _fail(f"{path}.percent", f"must be between 1 and 99, got {f.get('percent')!r}")

    window = _opt_int(f, "window", path, minimum=2)

    skew_s = f.get("skew_s")
    if fault_type is FaultType.CLOCK_SKEW:
        if isinstance(skew_s, bool) or not isinstance(skew_s, int) or skew_s == 0:
            raise _fail(f"{path}.skew_s", f"must be a non-zero integer of seconds, got {skew_s!r}")

    schedule = f.get("schedule")
    if fault_type is FaultType.INTERLEAVE:
        if agents < 2:
            raise _fail(path, "'interleave' needs 'agents: 2' or more at the top level")
        if schedule is not None and (
            not isinstance(schedule, list)
            or not schedule
            or not all(
                isinstance(s, int) and not isinstance(s, bool) and 0 <= s < agents for s in schedule
            )
        ):
            raise _fail(
                f"{path}.schedule",
                f"must be a non-empty list of agent indices 0..{agents - 1}, got {schedule!r}",
            )

    return FaultConfig(
        type=fault_type,
        on=on,
        event=event,
        occurrence=_opt_int(f, "occurrence", path),
        times=_opt_int(f, "times", path),
        ms=_opt_int(f, "ms", path),
        window=window,
        delay_s=_opt_int(f, "delay_s", path),
        percent=percent,
        skew_s=skew_s,
        schedule=list(schedule) if schedule is not None else None,
    )


def parse_scenario_data(raw: dict[str, Any]) -> ScenarioConfig:
    """Validate and convert a raw dict into a ScenarioConfig.

    Raises :class:`ScenarioValidationError` naming the offending field.
    """
    if not isinstance(raw, dict):
        raise ScenarioValidationError("scenario file must be a mapping of fields")

    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _fail("scenario", f"unknown field(s) {unknown}. Allowed: {sorted(_TOP_LEVEL_KEYS)}")

    name = raw.get("name")
    if not name:
        raise _fail("name", "required field is missing")

    budget_usd = _parse_budget(raw.get("budget_usd"))

    task = raw.get("task")
    if not task or not isinstance(task, str):
        raise _fail("task", "required field is missing (a description like 'buy one pack at $50')")

    if raw.get("seed") is None:
        raise _fail("seed", "required field is missing (determinism is required)")
    seed = _parse_int(raw["seed"], "seed", minimum=0)

    currency = str(raw.get("currency", "usd")).strip().lower()
    try:
        require_usd(currency, "currency")
    except UnsupportedCurrencyError as exc:
        raise ScenarioValidationError(str(exc)) from None

    concurrency = _parse_int(raw.get("concurrency", 1), "concurrency")
    agents = _parse_int(raw.get("agents", 1), "agents")

    raw_asserts = raw.get("assert", raw.get("assertions")) or []
    if not isinstance(raw_asserts, list):
        raise _fail("assert", f"must be a list of invariant ids, got {raw_asserts!r}")
    assertions: list[str] = []
    for i, a in enumerate(raw_asserts):
        a_id = str(a).strip().upper()
        if a_id not in VALID_INVARIANTS:
            raise _fail(
                f"assert[{i}]",
                f"unknown invariant {a!r}. Valid invariants: "
                f"{sorted(VALID_INVARIANTS, key=lambda x: int(x[1:]))}",
            )
        assertions.append(a_id)

    raw_faults = raw.get("faults") or []
    if not isinstance(raw_faults, list):
        raise _fail("faults", f"must be a list, got {raw_faults!r}")
    faults = [_parse_fault(f, i, agents) for i, f in enumerate(raw_faults)]

    capture_method = str(raw.get("capture_method", "automatic")).strip().lower()
    if capture_method not in ("automatic", "manual"):
        raise _fail("capture_method", f"must be 'automatic' or 'manual', got {capture_method!r}")

    webhook_source = str(raw.get("webhook_source", "synthetic")).strip().lower()
    if webhook_source not in ("synthetic", "stripe_cli"):
        raise _fail(
            "webhook_source", f"must be 'synthetic' or 'stripe_cli', got {webhook_source!r}"
        )
    cli_triggers = raw.get("cli_triggers") or []
    if not isinstance(cli_triggers, list) or not all(isinstance(t, str) for t in cli_triggers):
        raise _fail("cli_triggers", f"must be a list of event names, got {cli_triggers!r}")
    if webhook_source == "stripe_cli" and not cli_triggers:
        raise _fail("cli_triggers", "webhook_source: stripe_cli needs at least one trigger")

    settle = _parse_bool(raw.get("settle", False), "settle")
    refund = _parse_bool(raw.get("refund", False), "refund")
    if refund and capture_method == "manual" and not settle:
        raise _fail("refund", "with capture_method: manual, refund also needs settle: true")

    return ScenarioConfig(
        name=str(name),
        budget_usd=budget_usd,
        task=task,
        seed=seed,
        faults=faults,
        assertions=assertions,
        concurrency=concurrency,
        capture_method=capture_method,
        abort=_parse_bool(raw.get("abort", False), "abort"),
        settle=settle,
        refund=refund,
        currency=currency,
        agents=agents,
        webhook_source=webhook_source,
        cli_triggers=list(cli_triggers),
        cli_timeout_s=_parse_int(raw.get("cli_timeout_s", 60), "cli_timeout_s"),
    )


def load_scenario(path: str | Path) -> ScenarioConfig:
    """Load a scenario from a YAML or JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    content = p.read_text(encoding="utf-8")
    if HAS_YAML and yaml is not None:
        try:
            raw = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise ScenarioValidationError(f"{p.name}: not valid YAML: {exc}") from None
    else:
        raw = _simple_yaml_parse(content)

    return parse_scenario_data(raw)
