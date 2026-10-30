"""OpenAI Responses API usage normalization and cost estimation.

This module is deliberately UI-independent.  Provider responses and old project
files are untrusted inputs here: public helpers always return displayable data
instead of raising because a usage field is missing or malformed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from decimal import Decimal
import math
from typing import Any


PRICING_AS_OF = "2026-10-07"
PRICING_BASIS = "Standard short-context, USD per 1M tokens"
PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing?tab=suite"

# input, cached input, cache write, output (USD per one million tokens).
MODEL_PRICING_USD_PER_MILLION: dict[str, dict[str, Decimal]] = {
    "gpt-6-astra": {
        "input": Decimal("10"),
        "cached": Decimal("1"),
        "cache_write": Decimal("12.50"),
        "output": Decimal("50"),
    },
    "gpt-6-sol": {
        "input": Decimal("2"),
        "cached": Decimal("0.20"),
        "cache_write": Decimal("2.50"),
        "output": Decimal("10"),
    },
    "gpt-6.1-sol": {
        "input": Decimal("2"),
        "cached": Decimal("0.10"),
        "cache_write": Decimal("2.50"),
        "output": Decimal("10"),
    },
    "gpt-6-luna": {
        "input": Decimal("0.10"),
        "cached": Decimal("0.01"),
        "cache_write": Decimal("0.125"),
        "output": Decimal("0.50"),
    },
}

USAGE_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

SUCCESSFUL_RUN_STATUSES = frozenset(
    {
        "ready_for_review",
        "reviewed",
        "completed_empty",
        "completed",
        "succeeded",
        "success",
        # A completed request can later become obsolete when its crop changes.
        # Its saved usage is still real API usage and must remain in totals.
        "stale",
    }
)


def _token_count(value: Any) -> int:
    """Coerce a provider value to a non-negative whole token count."""

    if isinstance(value, bool) or value is None:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return int(number)


def _is_token_count(value: Any) -> bool:
    """Return whether a provider value is a usable whole token count."""

    if isinstance(value, bool) or value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number >= 0 and number.is_integer()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def normalize_usage(usage: Any) -> dict[str, int]:
    """Return a stable token dictionary from a Responses API usage payload.

    Both current nested detail fields and flat fields saved by older adapters
    are accepted. Invalid or negative values become zero.
    """

    source = _mapping(usage)
    input_details = _mapping(source.get("input_tokens_details"))
    output_details = _mapping(source.get("output_tokens_details"))

    input_tokens = _token_count(source.get("input_tokens"))
    cached_tokens = _token_count(
        _first_value(source.get("cached_tokens"), input_details.get("cached_tokens"))
    )
    cache_write_tokens = _token_count(
        _first_value(
            source.get("cache_write_tokens"),
            input_details.get("cache_write_tokens"),
            input_details.get("cache_creation_tokens"),
            source.get("cache_creation_input_tokens"),
        )
    )
    output_tokens = _token_count(source.get("output_tokens"))
    reasoning_tokens = _token_count(
        _first_value(source.get("reasoning_tokens"), output_details.get("reasoning_tokens"))
    )
    supplied_total = _token_count(source.get("total_tokens"))

    # total_tokens is informational. Derive it if absent, but do not add
    # cached/cache-write or reasoning details because their parent counts
    # normally already include them.
    total_tokens = supplied_total or (input_tokens + output_tokens)
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _has_usage_data(usage: Any) -> bool:
    if not isinstance(usage, Mapping):
        return False
    direct_fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    )
    if any(_is_token_count(usage.get(key)) for key in direct_fields if key in usage):
        return True

    input_details = _mapping(usage.get("input_tokens_details"))
    output_details = _mapping(usage.get("output_tokens_details"))
    detail_values = (
        input_details.get("cached_tokens"),
        input_details.get("cache_write_tokens"),
        input_details.get("cache_creation_tokens"),
        output_details.get("reasoning_tokens"),
    )
    return any(_is_token_count(value) for value in detail_values)


def _base_result(model: Any, usage: Any) -> dict[str, Any]:
    return {
        "status": "usage_unavailable",
        "model": str(model).strip() if isinstance(model, str) else "",
        "pricing_as_of": PRICING_AS_OF,
        "pricing_basis": PRICING_BASIS,
        "pricing_source": PRICING_SOURCE_URL,
        "usage": normalize_usage(usage),
        "billable_tokens": None,
        "cost_breakdown_usd": None,
        "cost_usd": None,
    }


def estimate_usage_cost(usage: Any, model: Any) -> dict[str, Any]:
    """Estimate one response's USD cost without raising on bad input.

    ``input_tokens`` normally includes cached and cache-write tokens, so only
    its remaining portion is charged at the ordinary input rate.
    """

    result = _base_result(model, usage)
    if not _has_usage_data(usage):
        return result

    pricing = MODEL_PRICING_USD_PER_MILLION.get(result["model"])
    if pricing is None:
        result["status"] = "pricing_unavailable"
        return result

    normalized = result["usage"]
    uncached = max(
        0,
        normalized["input_tokens"]
        - normalized["cached_tokens"]
        - normalized["cache_write_tokens"],
    )
    billable = {
        "uncached_input_tokens": uncached,
        "cached_tokens": normalized["cached_tokens"],
        "cache_write_tokens": normalized["cache_write_tokens"],
        "output_tokens": normalized["output_tokens"],
    }
    divisor = Decimal(1_000_000)
    costs = {
        "input": Decimal(uncached) * pricing["input"] / divisor,
        "cached": Decimal(billable["cached_tokens"]) * pricing["cached"] / divisor,
        "cache_write": Decimal(billable["cache_write_tokens"])
        * pricing["cache_write"]
        / divisor,
        "output": Decimal(billable["output_tokens"]) * pricing["output"] / divisor,
    }
    result.update(
        {
            "status": "estimated",
            "rates_usd_per_million": {
                key: float(value) for key, value in pricing.items()
            },
            "billable_tokens": billable,
            "cost_breakdown_usd": {key: float(value) for key, value in costs.items()},
            "cost_usd": float(sum(costs.values(), Decimal(0))),
        }
    )
    return result


def estimate_recognition_run_cost(run: Any) -> dict[str, Any]:
    """Return a run's saved price snapshot, or estimate it from current rates.

    Saving the estimate with the recognition run is important because model
    prices can change.  Old or hand-edited projects are treated as untrusted;
    malformed snapshots are ignored and safely recalculated when possible.
    """

    if not isinstance(run, Mapping):
        return estimate_usage_cost(None, None)
    model = run.get("model")
    stored = run.get("cost_estimate")
    if isinstance(stored, Mapping):
        cost = stored.get("cost_usd")
        stored_model = stored.get("model")
        stored_usage = stored.get("usage")
        current_usage = run.get("usage")
        if (
            stored.get("status") == "estimated"
            and stored_model == model
            and _has_usage_data(stored_usage)
            and normalize_usage(stored_usage) == normalize_usage(current_usage)
            and isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and math.isfinite(float(cost))
            and float(cost) >= 0
        ):
            result = deepcopy(dict(stored))
            result["usage"] = normalize_usage(current_usage)
            return result
    return estimate_usage_cost(run.get("usage"), model)


def summarize_recognition_runs(recognition_runs: Any) -> dict[str, Any]:
    """Aggregate successful recognition runs that contain usage information.

    Runs with failed/in-progress status or absent usage are ignored. A stale
    run is included only when it contains usage, because changing a crop does
    not erase the cost of the request that already completed. Usage from
    an unknown model is retained in token totals, while its price is explicitly
    reported as unavailable instead of silently treated as free.
    """

    result: dict[str, Any] = {
        "status": "usage_unavailable",
        "pricing_as_of": PRICING_AS_OF,
        "pricing_basis": PRICING_BASIS,
        "pricing_source": PRICING_SOURCE_URL,
        "run_count": 0,
        "estimated_run_count": 0,
        "unpriced_run_count": 0,
        "usage": {field: 0 for field in USAGE_FIELDS},
        "cost_usd": None,
    }
    if isinstance(recognition_runs, (str, bytes, Mapping)) or not isinstance(
        recognition_runs, Iterable
    ):
        return result

    total_cost = Decimal(0)
    for run in recognition_runs:
        if not isinstance(run, Mapping):
            continue
        if run.get("status") not in SUCCESSFUL_RUN_STATUSES:
            continue
        usage = run.get("usage")
        if not _has_usage_data(usage):
            continue

        estimate = estimate_recognition_run_cost(run)
        result["run_count"] += 1
        for field in USAGE_FIELDS:
            result["usage"][field] += estimate["usage"][field]
        if estimate["status"] == "estimated":
            result["estimated_run_count"] += 1
            total_cost += Decimal(str(estimate["cost_usd"]))
        else:
            result["unpriced_run_count"] += 1

    if result["run_count"] == 0:
        return result
    if result["unpriced_run_count"]:
        result["status"] = "partial_estimate" if result["estimated_run_count"] else "pricing_unavailable"
    else:
        result["status"] = "estimated"
    result["cost_usd"] = float(total_cost) if result["estimated_run_count"] else None
    return result


__all__ = [
    "MODEL_PRICING_USD_PER_MILLION",
    "PRICING_AS_OF",
    "PRICING_BASIS",
    "PRICING_SOURCE_URL",
    "SUCCESSFUL_RUN_STATUSES",
    "estimate_recognition_run_cost",
    "estimate_usage_cost",
    "normalize_usage",
    "summarize_recognition_runs",
]
