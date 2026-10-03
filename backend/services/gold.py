"""Loading human gold records (never generated automatically)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_gold_by_sha(gold_dir: str | Path | None) -> dict[str, dict[str, Any]]:
    """Returns {document_sha256: gold}. Files whose ``reviewed_at`` is empty are treated as unfinished and skipped."""
    out: dict[str, dict[str, Any]] = {}
    if not gold_dir:
        return out
    p = Path(gold_dir)
    if not p.exists():
        return out
    for f in sorted(p.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sha = data.get("document_sha256")
        if not sha or not data.get("reviewed_at") or data.get("status") not in (None, "final"):
            continue
        out[sha] = data
    return out
