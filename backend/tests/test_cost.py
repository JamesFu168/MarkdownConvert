from domain.models import UsageRecord
from services.cost import ModelPrice, PricingTable, aggregate_costs, compute_cost, load_pricing


def _table():
    return PricingTable(version="t", currency="USD", models={"m": ModelPrice(input_per_1m=1.0, output_per_1m=4.0, cached_input_per_1m=0.5)})


def test_cached_and_reasoning_are_subsets_not_added_twice():
    usage = UsageRecord(input_tokens=1_000_000, output_tokens=100_000, cached_tokens=400_000, reasoning_tokens=60_000)
    c = compute_cost(usage, "m", _table())
    assert c["status"] == "measured"
    # 600k uncached * 1 + 400k cached * 0.5 + 100k output * 4 = 0.6 + 0.2 + 0.4
    assert abs(c["amount"] - 1.2) < 1e-9
    assert c["breakdown"]["output"]["reasoning_tokens_included"] == 60_000


def test_unknown_when_usage_missing_or_no_price():
    assert compute_cost(None, "m", _table())["status"] == "unknown"
    assert compute_cost(UsageRecord(input_tokens=1, output_tokens=1), "other", _table())["reason"] == "no_price_entry"
    assert compute_cost(UsageRecord(input_tokens=1, output_tokens=1), "m", _table(), is_test_double=True)["status"] == "excluded"


def test_aggregate_reports_partial():
    a = compute_cost(UsageRecord(input_tokens=1_000_000, output_tokens=0), "m", _table())
    b = compute_cost(None, "m", _table())
    agg = aggregate_costs([a, b])
    assert agg["status"] == "partial" and agg["calls_unknown"] == 1 and abs(agg["measured_amount"] - 1.0) < 1e-9
    assert aggregate_costs([b])["status"] == "unknown"


def test_shipped_pricing_table_is_empty(tmp_path):
    from services.config import REPO_DIR

    t = load_pricing(REPO_DIR / "evaluation" / "pricing.json")
    assert t.models == {}
    assert load_pricing(tmp_path / "missing.json").version == "none"
