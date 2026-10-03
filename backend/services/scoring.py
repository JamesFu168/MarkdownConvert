"""Scoring of an extraction against a human-built gold record.

Gold format (``contracts/gold.schema.json``)::

    {
      "document_sha256": "...", "schema_version": "pilot-v1", "reviewer": "...", "reviewed_at": "...",
      "fields": {
        "document.product_name": {"status": "found", "value": "X", "accepted_values": ["X", "x"]},
        "document.revision_date": {"status": "found", "value": "2024-01-31"},
        "hazards.signal_word":    {"status": "found", "value": "Danger"},
        "physical.flash_point":   {"status": "found", "value": {"comparator": "gt", "min": 93, "max": null, "unit": "C"}}
      },
      "sets": {"hazards.h_codes": ["H225"], "hazards.p_codes": [...], "hazards.classifications": [...]},
      "ingredients": [{"name": "...", "accepted_names": [...], "cas": ["64-17-5"], "concentration": {"comparator":"range","min":10,"max":20,"unit":"%"}}]
    }

Rules: scalar fields are exact/normalised matches; absent statuses (not_reported / no_data /
not_applicable) are scored separately from valued fields; set fields use stable one-to-one
matching to compute TP/FP/FN; ingredient rows are matched one-to-one (CAS, then name) and a
row counts only when name, CAS and concentration are all correct. Extra rows are counted.
A missing value counts as wrong. No model self-assessment is used.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from domain.extraction_schema import SET_FIELD_PATHS, SdsExtraction
from domain.validation import normalize_cas

ABSENT = ("not_reported", "no_data", "not_applicable")
_PUNCT_EDGE = re.compile(r"^[\s\-–—:：,.;()\[\]\"'“”‘’]+|[\s\-–—:：,.;()\[\]\"'“”‘’]+$")


def norm_text(s: str | None) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return _PUNCT_EDGE.sub("", s)


def _get_path(result: SdsExtraction, path: str) -> Any:
    node: Any = result
    for part in path.split("."):
        node = getattr(node, part)
    return node


def _numeric_equal(a: dict[str, Any] | None, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    bd = b.model_dump() if hasattr(b, "model_dump") else dict(b)
    for k in ("comparator", "unit"):
        if (a.get(k) or None) != (bd.get(k) or None):
            return False
    for k in ("min", "max"):
        x, y = a.get(k), bd.get(k)
        if (x is None) != (y is None):
            return False
        if x is not None and abs(float(x) - float(y)) > 1e-6:
            return False
    return True


def _scalar_match(gold: dict[str, Any], env: Any, path: str) -> tuple[bool, str]:
    g_status = gold.get("status", "found")
    if g_status in ABSENT:
        return env.status == g_status, "absent"
    if g_status != "found":
        return env.status == g_status, "other"
    if env.status != "found":
        return False, "valued"
    if path == "physical.flash_point":
        return _numeric_equal(gold.get("value"), env.value), "valued"
    accepted = {norm_text(v) for v in ([gold.get("value")] + list(gold.get("accepted_values") or [])) if v is not None}
    return norm_text(env.value) in accepted and norm_text(env.value) != "", "valued"


def _set_match(gold_items: list[str], sys_items: list[str], normalizer) -> dict[str, int]:
    g = [normalizer(x) for x in gold_items]
    s = [normalizer(x) for x in sys_items]
    used = [False] * len(s)
    tp = 0
    for gi in g:  # stable one-to-one: each gold item consumes at most one system item
        for j, sj in enumerate(s):
            if not used[j] and sj == gi:
                used[j] = True
                tp += 1
                break
    return {"tp": tp, "fp": len(s) - tp, "fn": len(g) - tp}


def _ingredient_pair_score(g: dict[str, Any], ing: Any) -> int:
    score = 0
    g_cas = {normalize_cas(c) for c in (g.get("cas") or [])}
    s_cas = {normalize_cas(c) for c in ing.cas_numbers.value}
    if g_cas and g_cas == s_cas:
        score += 4
    elif g_cas & s_cas:
        score += 2
    names = {norm_text(g.get("name"))} | {norm_text(n) for n in (g.get("accepted_names") or [])}
    if norm_text(ing.name.value) in names and norm_text(ing.name.value):
        score += 2
    if _numeric_equal(g.get("concentration"), ing.concentration.value):
        score += 1
    return score


def score_extraction(result: SdsExtraction, gold: dict[str, Any]) -> dict[str, Any]:
    fields_out: dict[str, Any] = {}
    valued_correct = valued_total = absent_correct = absent_total = 0
    critical: dict[str, dict[str, int]] = {k: {"correct": 0, "total": 0} for k in
                                           ("ingredients.cas_numbers", "ingredients.concentration", "hazards.classifications", "physical.flash_point")}
    all_required_ok = True

    for path, g in (gold.get("fields") or {}).items():
        env = _get_path(result, path)
        ok, kind = _scalar_match(g, env, path)
        fields_out[path] = {"correct": ok, "kind": kind, "gold_status": g.get("status", "found"), "system_status": env.status,
                            "system_value": env.value.model_dump() if hasattr(env.value, "model_dump") else env.value}
        if kind == "valued":
            valued_total += 1
            valued_correct += int(ok)
        elif kind == "absent":
            absent_total += 1
            absent_correct += int(ok)
        if path in critical:
            critical[path]["total"] += 1
            critical[path]["correct"] += int(ok)
        all_required_ok &= ok

    sets_out: dict[str, dict[str, int]] = {}
    for path in SET_FIELD_PATHS:
        gold_items = (gold.get("sets") or {}).get(path)
        if gold_items is None:
            continue
        env = _get_path(result, path)
        normalizer = (lambda x: norm_text(x).replace(" ", "")) if path.endswith("codes") else norm_text
        m = _set_match(list(gold_items), list(env.value), normalizer)
        sets_out[path] = m
        if path == "hazards.classifications":
            critical[path]["total"] += len(gold_items)
            critical[path]["correct"] += m["tp"]
        all_required_ok &= (m["fp"] == 0 and m["fn"] == 0)

    gold_rows = list(gold.get("ingredients") or [])
    sys_rows = list(result.ingredients)
    pairs = []
    for gi, g in enumerate(gold_rows):
        for si, s in enumerate(sys_rows):
            sc = _ingredient_pair_score(g, s)
            if sc > 0:
                pairs.append((-sc, gi, si))
    pairs.sort()
    g_used: set[int] = set()
    s_used: set[int] = set()
    matched: list[tuple[int, int]] = []
    for _, gi, si in pairs:
        if gi in g_used or si in s_used:
            continue
        g_used.add(gi)
        s_used.add(si)
        matched.append((gi, si))

    rows_out = []
    rows_correct = 0
    for gi, g in enumerate(gold_rows):
        si = next((s for (gg, s) in matched if gg == gi), None)
        if si is None:
            rows_out.append({"gold_index": gi, "system_index": None, "correct": False, "reason": "missing"})
            critical["ingredients.cas_numbers"]["total"] += 1
            critical["ingredients.concentration"]["total"] += 1
            continue
        s = sys_rows[si]
        names = {norm_text(g.get("name"))} | {norm_text(n) for n in (g.get("accepted_names") or [])}
        name_ok = norm_text(s.name.value) in names and bool(norm_text(s.name.value))
        cas_ok = {normalize_cas(c) for c in (g.get("cas") or [])} == {normalize_cas(c) for c in s.cas_numbers.value}
        conc_ok = _numeric_equal(g.get("concentration"), s.concentration.value)
        ok = name_ok and cas_ok and conc_ok
        rows_correct += int(ok)
        critical["ingredients.cas_numbers"]["total"] += 1
        critical["ingredients.cas_numbers"]["correct"] += int(cas_ok)
        critical["ingredients.concentration"]["total"] += 1
        critical["ingredients.concentration"]["correct"] += int(conc_ok)
        rows_out.append({"gold_index": gi, "system_index": si, "correct": ok, "name_ok": name_ok, "cas_ok": cas_ok, "conc_ok": conc_ok})
    extra_rows = len(sys_rows) - len(s_used)
    if gold_rows:
        all_required_ok &= (rows_correct == len(gold_rows) and extra_rows == 0)

    metrics = {
        "valued_correct": valued_correct, "valued_total": valued_total,
        "absent_correct": absent_correct, "absent_total": absent_total,
        "sets": sets_out,
        "ingredient_rows_correct": rows_correct, "ingredient_rows_gold": len(gold_rows), "ingredient_rows_extra": extra_rows,
        "critical": critical,
        "document_pass": bool(all_required_ok),
    }
    return {"schema_version": gold.get("schema_version"), "fields": fields_out, "ingredient_rows": rows_out, "metrics": metrics}
