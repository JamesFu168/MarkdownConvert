from domain.extraction_schema import (
    ConcentrationField,
    ConcentrationValue,
    Evidence,
    FlashPointField,
    FlashPointValue,
    Ingredient,
    StringListField,
    TextField,
    empty_extraction,
)
from domain.validation import (
    cas_checksum_ok,
    cas_format_ok,
    normalize_cas,
    parse_concentration,
    parse_flash_point,
    review_required,
    validate_extraction,
)


def test_cas_checksum():
    assert cas_checksum_ok("7732-18-5")
    assert cas_checksum_ok("64-17-5")
    assert not cas_checksum_ok("64-17-6")
    assert not cas_format_ok("64175")
    assert normalize_cas("7732‐18‐5") == "7732-18-5"


def test_concentration_parsing_keeps_comparators_and_ranges():
    r = parse_concentration("10 - 20 %")
    assert (r.comparator, r.min, r.max, r.unit) == ("range", 10.0, 20.0, "%")
    r = parse_concentration("≤ 5 % w/w")
    assert (r.comparator, r.min, r.max, r.basis) == ("lte", None, 5.0, "w/w")
    r = parse_concentration("< 0.1%")
    assert (r.comparator, r.max) == ("lt", 0.1)
    assert parse_concentration("not reported") is None


def test_flash_point_parsing():
    fp = parse_flash_point("> 93 °C (closed cup)")
    assert (fp.comparator, fp.min, fp.unit, fp.conditions) == ("gt", 93.0, "C", "closed cup")
    fp = parse_flash_point("101 °F Tag closed cup")
    assert (fp.unit, fp.min) == ("F", 101.0)
    fp = parse_flash_point("約 40 ℃")
    assert (fp.comparator, fp.min, fp.unit) == ("approx", 40.0, "C")
    assert parse_flash_point("No data available") is None


def _found(value, raw, quote="q"):
    return TextField(value=value, raw_text=raw, status="found", evidence=[Evidence(quote=quote)])


def test_findings_for_missing_evidence_and_bad_codes():
    e = empty_extraction()
    e.document.product_name = TextField(value="X", raw_text="X", status="found", evidence=[])
    e.hazards.h_codes = StringListField(value=["H225", "HXYZ"], raw_text="H225", status="found", evidence=[Evidence(quote="H225")])
    e.hazards.p_codes = StringListField(value=["P210", "210"], status="found", evidence=[Evidence(quote="P210")])
    codes = {f.code for f in validate_extraction(e)}
    assert {"missing_evidence", "h_code_invalid_format", "p_code_invalid_format"} <= codes
    assert review_required(validate_extraction(e))


def test_ingredient_rules():
    e = empty_extraction()
    e.ingredients = [Ingredient(
        name=TextField(status="unreadable"),
        cas_numbers=StringListField(value=["64-17-6"], status="found", evidence=[Evidence(quote="64-17-6")]),
        concentration=ConcentrationField(value=ConcentrationValue(comparator="eq", min=50, max=50, unit="%"), raw_text="10 - 20 %",
                                         status="found", evidence=[Evidence(quote="10 - 20 %")]),
    )]
    codes = {f.code for f in validate_extraction(e)}
    assert "cas_checksum_failed" in codes
    assert "ingredient_name_missing" in codes
    assert "value_raw_mismatch" in codes


def test_clean_result_has_no_review_required_findings():
    e = empty_extraction()
    e.document.product_name = _found("Test", "Test")
    e.ingredients = [Ingredient(
        name=_found("Ethanol", "Ethanol"),
        cas_numbers=StringListField(value=["64-17-5"], raw_text="64-17-5", status="found", evidence=[Evidence(quote="64-17-5")]),
        concentration=ConcentrationField(value=ConcentrationValue(comparator="range", min=10, max=20, unit="%"), raw_text="10 - 20 %",
                                         status="found", evidence=[Evidence(quote="10 - 20 %")]),
    )]
    e.physical.flash_point = FlashPointField(value=FlashPointValue(comparator="gt", min=93, unit="C", conditions="closed cup"),
                                             raw_text="> 93 °C (closed cup)", status="found", evidence=[Evidence(quote="> 93")])
    findings = validate_extraction(e)
    assert not review_required(findings), [f.code for f in findings]


def test_absent_status_with_value_flagged():
    e = empty_extraction()
    e.physical.flash_point = FlashPointField(value=FlashPointValue(comparator="eq", min=1, max=1, unit="C"), raw_text="no data", status="no_data")
    assert "value_with_absent_status" in {f.code for f in validate_extraction(e)}
