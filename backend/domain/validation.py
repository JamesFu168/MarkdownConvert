"""Programmatic validation of an extraction result.

Schema conformance is checked separately (Pydantic). These rules produce *findings*
about content: CAS format/checksum, H/P code format, pairing of ingredient rows,
numeric normalisation consistency and presence of evidence. Findings never invent
information; they only flag what a human must review.

Also hosts the small parsers for concentration and flash-point text that both the
validator and the evaluation scorer share (``parse_concentration``, ``parse_flash_point``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .extraction_schema import (
    ConcentrationValue,
    FlashPointValue,
    SdsExtraction,
)

Severity = Literal["info", "warning", "error"]

CAS_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
H_CODE_RE = re.compile(r"^(?:H\d{3}[A-Za-z]{0,2}|EUH\d{3}[A-Za-z]?)$")
P_CODE_RE = re.compile(r"^P\d{3}(?:\+P\d{3})*$")

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_RANGE_SEP = r"\s*(?:-|–|—|~|to|至|〜)\s*"
_UNIT = r"(%|wt\.?%|w/w\s*%|vol\.?%|ppm|ppb|mg/m3|mg/m³|g/l|g/L|mg/l|mg/L)?"
_BASIS = r"\(?\s*(w/w|v/v|w/v|wt|vol)\s*\)?"


@dataclass
class Finding:
    code: str
    severity: Severity
    path: str
    message: str
    review_required: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "review_required": self.review_required,
            "data": self.data,
        }


# --------------------------------------------------------------------------- CAS

def normalize_cas(text: str) -> str:
    return re.sub(r"[\s‐-―−]", lambda m: "-" if m.group(0) not in " \t" else "", text.strip()).replace("--", "-")


def cas_format_ok(cas: str) -> bool:
    return bool(CAS_RE.match(cas))


def cas_checksum_ok(cas: str) -> bool:
    """CAS check digit: sum of digits weighted by position from the right (excluding the check digit) mod 10.

    A valid checksum does NOT prove the number belongs to the named ingredient.
    """
    m = CAS_RE.match(cas)
    if not m:
        return False
    digits = (m.group(1) + m.group(2))[::-1]
    total = sum(int(d) * (i + 1) for i, d in enumerate(digits))
    return total % 10 == int(m.group(3))


# ------------------------------------------------------------------- Concentration

_CONC_RANGE = re.compile(rf"^\s*(?:ca\.?|approx\.?|約)?\s*({_NUM}){_RANGE_SEP}({_NUM})\s*{_UNIT}\s*(?:{_BASIS})?\s*$", re.I)
_CONC_CMP = re.compile(rf"^\s*(<=|>=|≤|≥|<|>|=|~|≈|ca\.?|approx\.?|約|up to|max\.?|min\.?)?\s*({_NUM})\s*{_UNIT}\s*(?:{_BASIS})?\s*$", re.I)

_CMP_MAP = {
    "<": "lt", "<=": "lte", "≤": "lte", ">": "gt", ">=": "gte", "≥": "gte", "=": "eq", None: "eq", "": "eq",
    "~": "approx", "≈": "approx", "ca": "approx", "ca.": "approx", "approx": "approx", "approx.": "approx", "約": "approx",
    "up to": "lte", "max": "lte", "max.": "lte", "min": "gte", "min.": "gte",
}


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower().replace(" ", "")
    if u in ("%", "wt%", "wt.%", "w/w%", "vol%", "vol.%"):
        return "%"
    if u == "mg/m3":
        return "mg/m³"
    return u


def parse_concentration(raw: str | None) -> ConcentrationValue | None:
    """Parse '10 - 20 %', '≤ 5 % w/w', '< 0.1%', '90' into a ConcentrationValue. Returns None if unparseable."""
    if not raw:
        return None
    text = raw.strip()
    m = _CONC_RANGE.match(text)
    if m:
        lo, hi, unit, basis = m.groups()
        return ConcentrationValue(comparator="range", min=_num(lo), max=_num(hi), unit=_unit(unit), basis=basis.lower() if basis else None)
    m = _CONC_CMP.match(text)
    if m:
        cmp_raw, val, unit, basis = m.groups()
        cmp_key = cmp_raw.lower().strip() if cmp_raw else None
        comparator = _CMP_MAP.get(cmp_key, None)
        if comparator is None:
            return None
        v = _num(val)
        lo = v if comparator in ("eq", "approx", "gt", "gte") else None
        hi = v if comparator in ("eq", "approx", "lt", "lte") else None
        return ConcentrationValue(comparator=comparator, min=lo, max=hi, unit=_unit(unit), basis=basis.lower() if basis else None)
    return None


# ---------------------------------------------------------------------- Flash point

_FP_RE = re.compile(
    rf"^\s*(<=|>=|≤|≥|<|>|~|≈|ca\.?|approx\.?|約)?\s*({_NUM})(?:{_RANGE_SEP}({_NUM}))?\s*(°\s*C|°\s*F|℃|℉|deg\s*C|deg\s*F|C|F)(?![A-Za-z])(.*)$",
    re.I,
)
_FP_NO_DATA = re.compile(r"(no data|not available|無資料|沒有資料|n\.?/?a\b|不適用|not applicable|none)", re.I)


def parse_flash_point(raw: str | None) -> FlashPointValue | None:
    if not raw:
        return None
    m = _FP_RE.match(raw.strip())
    if not m:
        return None
    cmp_raw, v1, v2, unit_raw, rest = m.groups()
    unit = "C" if "c" in unit_raw.lower() or "℃" in unit_raw else "F"
    cond = rest.strip(" ,;()") or None
    if v2 is not None:
        return FlashPointValue(comparator="range", min=_num(v1), max=_num(v2), unit=unit, conditions=cond)
    comparator = _CMP_MAP.get(cmp_raw.lower().strip() if cmp_raw else None, "eq")
    v = _num(v1)
    lo = v if comparator in ("eq", "approx", "gt", "gte") else None
    hi = v if comparator in ("eq", "approx", "lt", "lte") else None
    return FlashPointValue(comparator=comparator, min=lo, max=hi, unit=unit, conditions=cond)


def flash_point_text_is_no_data(raw: str | None) -> bool:
    return bool(raw and _FP_NO_DATA.search(raw))


# ---------------------------------------------------------------------- Validation

def _env_checks(path: str, env: Any, findings: list[Finding], *, has_value: bool) -> None:
    status = env.status
    if status == "found":
        if not has_value and not env.raw_text:
            findings.append(Finding("found_without_value", "error", path, "status=found 但 value 與 raw_text 皆為空"))
        if not env.evidence:
            findings.append(Finding("missing_evidence", "warning", path, "status=found 但沒有來源 evidence"))
    elif status == "conflict":
        findings.append(Finding("conflicting_values", "warning", path, "文件內出現矛盾值，需人工核對"))
    elif status in ("not_reported", "no_data", "not_applicable") and has_value:
        findings.append(Finding("value_with_absent_status", "warning", path, f"status={status} 但仍有 value"))


def validate_extraction(result: SdsExtraction) -> list[Finding]:
    findings: list[Finding] = []

    d = result.document
    for name in ("product_name", "revision_date", "supplier_name"):
        env = getattr(d, name)
        _env_checks(f"document.{name}", env, findings, has_value=env.value is not None)

    for i, ing in enumerate(result.ingredients):
        base = f"ingredients[{i}]"
        _env_checks(f"{base}.name", ing.name, findings, has_value=ing.name.value is not None)
        _env_checks(f"{base}.cas_numbers", ing.cas_numbers, findings, has_value=bool(ing.cas_numbers.value))
        _env_checks(f"{base}.concentration", ing.concentration, findings, has_value=ing.concentration.value is not None)

        for j, cas in enumerate(ing.cas_numbers.value):
            p = f"{base}.cas_numbers.value[{j}]"
            norm = normalize_cas(cas)
            if not cas_format_ok(norm):
                findings.append(Finding("cas_invalid_format", "error", p, f"CAS 格式不正確: {cas!r}", data={"cas": cas}))
            elif not cas_checksum_ok(norm):
                findings.append(Finding("cas_checksum_failed", "error", p, f"CAS 檢查碼錯誤: {cas!r}", data={"cas": cas}))
        if ing.cas_numbers.value and ing.name.status != "found":
            findings.append(Finding("ingredient_name_missing", "warning", f"{base}.name", "有 CAS 但成分名稱未抽出，配對不確定"))
        if ing.name.status == "found" and ing.concentration.status == "found" and not ing.concentration.raw_text:
            findings.append(Finding("concentration_raw_missing", "error", f"{base}.concentration", "濃度 status=found 但缺少原文 raw_text"))
        if ing.concentration.raw_text and ing.concentration.value is not None:
            parsed = parse_concentration(ing.concentration.raw_text)
            if parsed is not None and not _range_equal(parsed, ing.concentration.value):
                findings.append(Finding("value_raw_mismatch", "warning", f"{base}.concentration",
                                        "正規化濃度與原文解析結果不一致",
                                        data={"parsed": parsed.model_dump(), "model": ing.concentration.value.model_dump()}))

    h = result.hazards
    _env_checks("hazards.classifications", h.classifications, findings, has_value=bool(h.classifications.value))
    _env_checks("hazards.signal_word", h.signal_word, findings, has_value=h.signal_word.value is not None)
    _env_checks("hazards.h_codes", h.h_codes, findings, has_value=bool(h.h_codes.value))
    _env_checks("hazards.p_codes", h.p_codes, findings, has_value=bool(h.p_codes.value))
    for j, code in enumerate(h.h_codes.value):
        if not H_CODE_RE.match(code.strip()):
            findings.append(Finding("h_code_invalid_format", "error", f"hazards.h_codes.value[{j}]", f"H code 格式不正確: {code!r}"))
    for j, code in enumerate(h.p_codes.value):
        if not P_CODE_RE.match(code.strip().replace(" ", "")):
            findings.append(Finding("p_code_invalid_format", "error", f"hazards.p_codes.value[{j}]", f"P code 格式不正確: {code!r}"))
    if h.signal_word.value and h.signal_word.value.strip().lower() not in ("danger", "warning", "危險", "警告"):
        findings.append(Finding("signal_word_unexpected", "warning", "hazards.signal_word", f"警示語非預期值: {h.signal_word.value!r}"))

    fp = result.physical.flash_point
    _env_checks("physical.flash_point", fp, findings, has_value=fp.value is not None)
    if fp.status == "found" and fp.value is not None:
        if fp.value.unit is None:
            findings.append(Finding("flash_point_unit_missing", "warning", "physical.flash_point", "閃火點缺少 °C／°F 單位"))
        if fp.raw_text:
            parsed = parse_flash_point(fp.raw_text)
            if parsed is not None and not _fp_equal(parsed, fp.value):
                findings.append(Finding("value_raw_mismatch", "warning", "physical.flash_point", "正規化閃火點與原文解析結果不一致",
                                        data={"parsed": parsed.model_dump(), "model": fp.value.model_dump()}))
    if fp.status == "found" and fp.raw_text and flash_point_text_is_no_data(fp.raw_text) and fp.value is None:
        findings.append(Finding("absent_marked_found", "warning", "physical.flash_point", "原文為無資料／不適用，但 status 標為 found"))

    return findings


def _range_equal(a: ConcentrationValue, b: ConcentrationValue) -> bool:
    return (a.comparator == b.comparator and _feq(a.min, b.min) and _feq(a.max, b.max)
            and (a.unit or None) == (b.unit or None))


def _fp_equal(a: FlashPointValue, b: FlashPointValue) -> bool:
    return a.comparator == b.comparator and _feq(a.min, b.min) and _feq(a.max, b.max) and a.unit == b.unit


def _feq(x: float | None, y: float | None) -> bool:
    if x is None or y is None:
        return x is y
    return abs(x - y) < 1e-6


def review_required(findings: list[Finding]) -> bool:
    return any(f.review_required and f.severity in ("warning", "error") for f in findings)
