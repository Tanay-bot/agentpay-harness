"""Scenario loader and validator for agentpay-harness.

Loads YAML or JSON scenario definition files, validates invariant IDs and fault
configurations, and produces typed :class:`ScenarioConfig` objects.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from agentpay_harness.types import FaultConfig, FaultType, ScenarioConfig

# Optional PyYAML support with built-in lightweight fallback
try:
    import yaml

    HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    HAS_YAML = False

VALID_INVARIANTS = {"I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10", "I11"}

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
}


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


def parse_scenario_data(raw: dict[str, Any]) -> ScenarioConfig:
    """Validate and convert raw dict into a ScenarioConfig."""
    name = raw.get("name")
    if not name:
        raise ValueError("Scenario missing required field 'name'")

    budget_raw = raw.get("budget_usd")
    if budget_raw is None:
        raise ValueError("Scenario missing required field 'budget_usd'")
    budget_usd = Decimal(str(budget_raw))

    task = raw.get("task")
    if not task:
        raise ValueError("Scenario missing required field 'task'")

    seed = raw.get("seed")
    if seed is None:
        raise ValueError("Scenario missing required field 'seed' (determinism is required)")

    concurrency = int(raw.get("concurrency", 1))

    # Assertions / invariants
    raw_asserts = raw.get("assert") or raw.get("assertions") or []
    assertions: list[str] = []
    for a in raw_asserts:
        a_id = str(a).strip().upper()
        if a_id not in VALID_INVARIANTS:
            raise ValueError(
                f"Unknown invariant '{a_id}'. Valid invariants: {sorted(VALID_INVARIANTS)}"
            )
        assertions.append(a_id)

    # Faults
    raw_faults = raw.get("faults") or []
    faults: list[FaultConfig] = []
    for f in raw_faults:
        if isinstance(f, FaultConfig):
            faults.append(f)
            continue
        if not isinstance(f, dict):
            continue

        raw_type = f.get("type")
        if not raw_type:
            raise ValueError(f"Fault entry missing 'type': {f}")
        type_str = str(raw_type).strip().lower()
        if type_str not in FAULT_TYPE_MAP:
            raise ValueError(
                f"Unknown fault type '{raw_type}'. Valid types: {sorted(FAULT_TYPE_MAP.keys())}"
            )

        fault_type = FAULT_TYPE_MAP[type_str]
        faults.append(
            FaultConfig(
                type=fault_type,
                on=f.get("on"),
                event=f.get("event"),
                occurrence=f.get("occurrence"),
                times=f.get("times"),
                ms=f.get("ms"),
                window=f.get("window"),
                delay_s=f.get("delay_s"),
            )
        )

    return ScenarioConfig(
        name=name,
        budget_usd=budget_usd,
        task=task,
        seed=int(seed),
        faults=faults,
        assertions=assertions,
        concurrency=concurrency,
    )


def load_scenario(path: str | Path) -> ScenarioConfig:
    """Load a scenario from a YAML or JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")

    content = p.read_text(encoding="utf-8")
    if HAS_YAML and yaml is not None:
        raw = yaml.safe_load(content) or {}
    else:
        raw = _simple_yaml_parse(content)

    return parse_scenario_data(raw)
