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
from services.scoring import score_extraction


def _ing(name, cas, conc):
    return Ingredient(
        name=TextField(value=name, raw_text=name, status="found", evidence=[Evidence(quote=name)]),
        cas_numbers=StringListField(value=cas, status="found", evidence=[Evidence(quote=cas[0] if cas else "")]),
        concentration=ConcentrationField(value=conc, raw_text="x", status="found", evidence=[Evidence(quote="x")]),
    )


GOLD = {
    "document_sha256": "abc", "schema_version": "pilot-v1", "reviewed_at": "2026-10-03",
    "fields": {
        "document.product_name": {"status": "found", "value": "Test Product", "accepted_values": ["TEST PRODUCT"]},
        "document.supplier_name": {"status": "not_reported"},
        "physical.flash_point": {"status": "found", "value": {"comparator": "gt", "min": 93, "max": None, "unit": "C"}},
    },
    "sets": {"hazards.h_codes": ["H225", "H319"], "hazards.p_codes": [], "hazards.classifications": ["Flammable liquids 2"]},
    "ingredients": [
        {"name": "Ethanol", "cas": ["64-17-5"], "concentration": {"comparator": "range", "min": 10, "max": 20, "unit": "%"}},
        {"name": "Water", "cas": ["7732-18-5"], "concentration": {"comparator": "gte", "min": 80, "max": None, "unit": "%"}},
    ],
}


def test_scoring_valued_absent_sets_and_rows():
    e = empty_extraction()
    e.document.product_name = TextField(value="test product", raw_text="Test Product", status="found", evidence=[Evidence(quote="x")])
    e.document.supplier_name = TextField(status="not_reported")
    e.physical.flash_point = FlashPointField(value=FlashPointValue(comparator="gt", min=93, unit="C"), raw_text=">93", status="found", evidence=[Evidence(quote="x")])
    e.hazards.h_codes = StringListField(value=["H225", "H225", "H999"], status="found", evidence=[Evidence(quote="x")])
    e.hazards.classifications = StringListField(value=["Flammable Liquids 2"], status="found", evidence=[Evidence(quote="x")])
    e.ingredients = [
        _ing("Water", ["7732-18-5"], ConcentrationValue(comparator="gte", min=80, unit="%")),
        _ing("Ethanol", ["64-17-5"], ConcentrationValue(comparator="eq", min=15, max=15, unit="%")),  # wrong concentration
        _ing("Extra", ["71-43-2"], ConcentrationValue(comparator="lt", max=1, unit="%")),
    ]
    s = score_extraction(e, GOLD)
    m = s["metrics"]
    assert (m["valued_correct"], m["valued_total"]) == (2, 2)
    assert (m["absent_correct"], m["absent_total"]) == (1, 1)
    # duplicates do not double count; H319 missing; H999 extra; one duplicate H225 is a FP
    assert m["sets"]["hazards.h_codes"] == {"tp": 1, "fp": 2, "fn": 1}
    assert m["sets"]["hazards.classifications"]["tp"] == 1
    assert m["ingredient_rows_correct"] == 1 and m["ingredient_rows_gold"] == 2 and m["ingredient_rows_extra"] == 1
    assert m["critical"]["ingredients.cas_numbers"] == {"correct": 2, "total": 2}
    assert m["critical"]["ingredients.concentration"] == {"correct": 1, "total": 2}
    assert m["document_pass"] is False


def test_missing_value_counts_as_wrong_and_absent_mismatch():
    e = empty_extraction()  # everything unreadable
    s = score_extraction(e, GOLD)
    m = s["metrics"]
    assert m["valued_correct"] == 0 and m["valued_total"] == 2
    assert m["absent_correct"] == 0 and m["absent_total"] == 1
    assert m["ingredient_rows_correct"] == 0 and m["ingredient_rows_gold"] == 2
