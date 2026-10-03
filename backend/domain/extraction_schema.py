"""Pilot extraction schema (version ``pilot-v1``).

The business field list was not supplied, so this pilot schema is used for the first
A/B/C/D comparison. Once used for an evaluation the version is frozen; later field
lists get a new version plus a mapping table.

Every field is an *envelope*: ``value`` (normalised, may be null), ``raw_text`` (verbatim
from the document), ``status`` and ``evidence``. Normalisation never discards the raw text
and never collapses "<", ranges or "not reported" into a single confident number.

``strict_json_schema()`` produces the subset accepted by OpenAI Structured Outputs
(all properties required, ``additionalProperties: false``, no defaults, ``const`` rewritten
as single-value ``enum``). ``tests/test_schema.py`` asserts those invariants.
"""
from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "pilot-v1"

FieldStatus = Literal["found", "not_reported", "no_data", "not_applicable", "unreadable", "conflict"]
FIELD_STATUSES: tuple[str, ...] = ("found", "not_reported", "no_data", "not_applicable", "unreadable", "conflict")

Comparator = Literal["eq", "lt", "lte", "gt", "gte", "approx", "range"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    """Where a value came from. Unknown locators are null; the model must not guess pages."""
    section: str | None = Field(default=None, description="SDS section number or heading, e.g. '3' or 'Section 9'")
    page: int | None = Field(default=None, description="1-based PDF page if verifiable, else null")
    block_id: str | None = Field(default=None, description="Block id given in the model input, e.g. 'b0042'")
    quote: str | None = Field(default=None, description="Verbatim quote copied from the document")


class TextField(_Strict):
    value: str | None = Field(default=None, description="Normalised value or null")
    raw_text: str | None = Field(default=None, description="Verbatim text from the document")
    status: FieldStatus = "unreadable"
    evidence: list[Evidence] = Field(default_factory=list)


class StringListField(_Strict):
    value: list[str] = Field(default_factory=list, description="Normalised items; empty when none")
    raw_text: str | None = None
    status: FieldStatus = "unreadable"
    evidence: list[Evidence] = Field(default_factory=list)


class ConcentrationValue(_Strict):
    comparator: Comparator | None = Field(default=None, description="eq, lt, lte, gt, gte, approx or range")
    min: float | None = Field(default=None, description="Lower bound (or the single value for eq/approx/gt/gte)")
    max: float | None = Field(default=None, description="Upper bound (or the single value for lt/lte)")
    unit: str | None = Field(default=None, description="e.g. '%', 'ppm', 'g/L'")
    basis: str | None = Field(default=None, description="e.g. 'w/w', 'v/v' when stated")


class ConcentrationField(_Strict):
    value: ConcentrationValue | None = None
    raw_text: str | None = None
    status: FieldStatus = "unreadable"
    evidence: list[Evidence] = Field(default_factory=list)


class FlashPointValue(_Strict):
    comparator: Comparator | None = None
    min: float | None = None
    max: float | None = None
    unit: Literal["C", "F"] | None = Field(default=None, description="C for °C, F for °F")
    conditions: str | None = Field(default=None, description="Test method / cup type if stated, verbatim")


class FlashPointField(_Strict):
    value: FlashPointValue | None = None
    raw_text: str | None = None
    status: FieldStatus = "unreadable"
    evidence: list[Evidence] = Field(default_factory=list)


class Ingredient(_Strict):
    """One ingredient row. Name, CAS numbers and concentration must stay paired."""
    name: TextField = Field(default_factory=TextField)
    cas_numbers: StringListField = Field(default_factory=StringListField)
    concentration: ConcentrationField = Field(default_factory=ConcentrationField)


class DocumentInfo(_Strict):
    product_name: TextField = Field(default_factory=TextField)
    revision_date: TextField = Field(default_factory=TextField, description="value as ISO date YYYY-MM-DD when unambiguous")
    supplier_name: TextField = Field(default_factory=TextField)


class HazardInfo(_Strict):
    classifications: StringListField = Field(default_factory=StringListField)
    signal_word: TextField = Field(default_factory=TextField)
    h_codes: StringListField = Field(default_factory=StringListField, description="Only codes explicitly printed, e.g. H225")
    p_codes: StringListField = Field(default_factory=StringListField, description="Only codes explicitly printed, e.g. P210")


class PhysicalInfo(_Strict):
    flash_point: FlashPointField = Field(default_factory=FlashPointField)


class SdsExtraction(_Strict):
    schema_version: Literal["pilot-v1"] = SCHEMA_VERSION
    document: DocumentInfo = Field(default_factory=DocumentInfo)
    ingredients: list[Ingredient] = Field(default_factory=list)
    hazards: HazardInfo = Field(default_factory=HazardInfo)
    physical: PhysicalInfo = Field(default_factory=PhysicalInfo)
    extraction_notes: str | None = Field(default=None, description="Short notes about ambiguity; never instructions")


# Paths of scalar / list fields used for scoring and CSV export (ingredients handled as rows).
SCALAR_FIELD_PATHS: tuple[str, ...] = (
    "document.product_name",
    "document.revision_date",
    "document.supplier_name",
    "hazards.signal_word",
    "physical.flash_point",
)
SET_FIELD_PATHS: tuple[str, ...] = (
    "hazards.classifications",
    "hazards.h_codes",
    "hazards.p_codes",
)
CRITICAL_FIELDS: tuple[str, ...] = ("ingredients.cas_numbers", "ingredients.concentration", "hazards.classifications", "physical.flash_point")


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    node = dict(node)
    node.pop("default", None)
    node.pop("title", None)
    if "const" in node:
        node["enum"] = [node.pop("const")]
    if node.get("type") == "object" and "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node["properties"].keys())
    for key in ("properties", "$defs"):
        if key in node:
            node[key] = {k: _strictify(v) for k, v in node[key].items()}
    for key in ("items", "additionalProperties"):
        if key in node and isinstance(node[key], dict):
            node[key] = _strictify(node[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in node:
            node[key] = [_strictify(n) for n in node[key]]
    return node


def strict_json_schema() -> dict[str, Any]:
    """JSON Schema for ``SdsExtraction`` in the OpenAI Structured Outputs strict subset."""
    raw = SdsExtraction.model_json_schema()
    schema = _strictify(copy.deepcopy(raw))
    schema["title"] = "SdsExtraction"
    return schema


def empty_extraction(status: FieldStatus = "unreadable") -> SdsExtraction:
    """A fully populated, schema-valid envelope with every field in ``status`` (used by test doubles)."""
    def t() -> TextField:
        return TextField(status=status)

    def l() -> StringListField:  # noqa: E743
        return StringListField(status=status)

    return SdsExtraction(
        document=DocumentInfo(product_name=t(), revision_date=t(), supplier_name=t()),
        ingredients=[],
        hazards=HazardInfo(classifications=l(), signal_word=t(), h_codes=l(), p_codes=l()),
        physical=PhysicalInfo(flash_point=FlashPointField(status=status)),
        extraction_notes=None,
    )
