"""Token cost computation from a configurable pricing table.

* Prices are NEVER hard-coded; they come from ``evaluation/pricing.json`` (currency, date,
  per-token-class unit prices). The shipped table has no entries, so cost is ``unknown``
  until the operator fills it from the official price list.
* ``cached_tokens`` is a subset of ``input_tokens`` and ``reasoning_tokens`` a subset of
  ``output_tokens``: they are billed differently but never added on top.
* Cost is only computed when usage was returned by the API. Timeouts / disconnects with
  unknown usage are ``unknown``, never zero. Test-double usage is ``excluded``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.models import UsageRecord


@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float | None = None
    effective_date: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class PricingTable:
    version: str
    currency: str
    models: dict[str, ModelPrice]

    @classmethod
    def empty(cls) -> PricingTable:
        return cls(version="none", currency="USD", models={})


def load_pricing(path: str | Path | None) -> PricingTable:
    if not path:
        return PricingTable.empty()
    p = Path(path)
    if not p.exists():
        return PricingTable.empty()
    data = json.loads(p.read_text(encoding="utf-8"))
    models = {}
    for name, m in (data.get("models") or {}).items():
        models[name] = ModelPrice(
            input_per_1m=float(m["input_per_1m"]),
            output_per_1m=float(m["output_per_1m"]),
            cached_input_per_1m=float(m["cached_input_per_1m"]) if m.get("cached_input_per_1m") is not None else None,
            effective_date=m.get("effective_date"),
            source=m.get("source"),
        )
    return PricingTable(version=str(data.get("version", "unversioned")), currency=str(data.get("currency", "USD")), models=models)


def compute_cost(usage: UsageRecord | None, model: str, table: PricingTable, *, is_test_double: bool = False) -> dict[str, Any]:
    base = {"currency": table.currency, "pricing_version": table.version, "model": model}
    if is_test_double:
        return {**base, "status": "excluded", "reason": "test_double", "amount": None}
    if usage is None or usage.input_tokens is None or usage.output_tokens is None:
        return {**base, "status": "unknown", "reason": "usage_missing", "amount": None}
    price = table.models.get(model)
    if price is None:
        return {**base, "status": "unknown", "reason": "no_price_entry", "amount": None,
                "tokens": {"input": usage.input_tokens, "cached_input": usage.cached_tokens or 0, "output": usage.output_tokens}}
    cached = min(usage.cached_tokens or 0, usage.input_tokens)
    uncached = usage.input_tokens - cached
    cached_rate = price.cached_input_per_1m if price.cached_input_per_1m is not None else price.input_per_1m
    reasoning = min(usage.reasoning_tokens or 0, usage.output_tokens)
    breakdown = {
        "uncached_input": {"tokens": uncached, "amount": uncached * price.input_per_1m / 1_000_000},
        "cached_input": {"tokens": cached, "amount": cached * cached_rate / 1_000_000},
        # reasoning tokens are already inside output_tokens; shown for transparency, NOT added again
        "output": {"tokens": usage.output_tokens, "reasoning_tokens_included": reasoning,
                   "amount": usage.output_tokens * price.output_per_1m / 1_000_000},
    }
    amount = sum(v["amount"] for v in breakdown.values())
    return {**base, "status": "measured", "amount": round(amount, 8), "breakdown": breakdown,
            "price_effective_date": price.effective_date}


def aggregate_costs(costs: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Sum measured amounts; report how many were unknown/excluded so partial totals are never presented as full."""
    measured = 0.0
    n_measured = n_unknown = n_excluded = 0
    currency = None
    for c in costs:
        if not c:
            n_unknown += 1
            continue
        currency = currency or c.get("currency")
        st = c.get("status")
        if st == "measured":
            measured += float(c.get("amount") or 0.0)
            n_measured += 1
        elif st == "excluded":
            n_excluded += 1
        else:
            n_unknown += 1
    if n_measured and not n_unknown:
        status = "measured"
    elif n_measured:
        status = "partial"
    else:
        status = "unknown"
    return {"status": status, "measured_amount": round(measured, 8), "currency": currency, "calls_measured": n_measured,
            "calls_unknown": n_unknown, "calls_excluded_test_double": n_excluded}
