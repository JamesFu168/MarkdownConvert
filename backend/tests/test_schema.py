import json

from domain.extraction_schema import SdsExtraction, empty_extraction, strict_json_schema


def _walk(node, path="root"):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, path
            assert set(node["required"]) == set(node["properties"]), path
        assert "default" not in node, path
        assert "const" not in node, path
        for k, v in node.items():
            _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]")


def test_strict_schema_invariants():
    schema = strict_json_schema()
    _walk(schema)
    assert schema["properties"]["schema_version"]["enum"] == ["pilot-v1"]
    assert "Ingredient" in schema["$defs"]
    # structured outputs limits: keep the schema reasonably small
    assert len(json.dumps(schema)) < 15_000


def test_empty_extraction_roundtrip_and_statuses():
    e = empty_extraction("unreadable")
    dumped = e.model_dump()
    SdsExtraction.model_validate(dumped)
    assert dumped["document"]["product_name"]["status"] == "unreadable"
    assert dumped["ingredients"] == []


def test_extra_keys_rejected():
    bad = empty_extraction().model_dump()
    bad["unexpected"] = 1
    try:
        SdsExtraction.model_validate(bad)
    except Exception as e:  # noqa: BLE001
        assert "unexpected" in str(e)
    else:
        raise AssertionError("extra keys must be rejected")
