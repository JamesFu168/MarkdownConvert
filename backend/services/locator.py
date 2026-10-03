"""Source location: Markdown block ids, offset map and quote verification.

* ``segment_markdown`` splits the MarkItDown output into blocks (paragraph / heading /
  table / code) and records exact character offsets into the ORIGINAL markdown.
* ``build_model_input`` prepends a ``[[b0001]]`` marker line to each block. Content is
  never rewritten or reordered; the marker is the only addition.
* ``verify_quote`` checks a model quote against verifiable sources using the versioned
  whitespace normalisation ``ws-v1`` (whitespace runs collapsed; digits, symbols and units
  are never removed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from domain.extraction_schema import Evidence, SdsExtraction
from domain.validation import Finding

WS_NORMALIZATION_VERSION = "ws-v1"
BLOCK_MARKER_RE = re.compile(r"^\[\[(b\d{4,})\]\]$")
_WS_RE = re.compile(r"[\s  -​　]+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


@dataclass
class Block:
    block_id: str
    start: int  # offset into original markdown (inclusive)
    end: int  # offset (exclusive)
    kind: str  # paragraph | heading | table | code

    def to_dict(self) -> dict[str, Any]:
        return {"block_id": self.block_id, "start": self.start, "end": self.end, "kind": self.kind}


def segment_markdown(md: str) -> list[Block]:
    blocks: list[Block] = []
    lines = md.split("\n")
    pos = 0
    cur_start: int | None = None
    cur_kind: str | None = None
    in_code = False

    def close(end: int) -> None:
        nonlocal cur_start, cur_kind
        if cur_start is not None and end > cur_start:
            blocks.append(Block(block_id=f"b{len(blocks) + 1:04d}", start=cur_start, end=end, kind=cur_kind or "paragraph"))
        cur_start, cur_kind = None, None

    for line in lines:
        line_start = pos
        line_end = pos + len(line)
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                in_code = False
                close(line_end)
            else:
                close(line_start - 1 if line_start else 0)
                in_code = True
                cur_start, cur_kind = line_start, "code"
        elif in_code:
            pass
        elif not stripped:
            close(line_start - 1 if line_start else 0)
        elif stripped.startswith("|"):
            if cur_kind != "table":
                close(line_start - 1 if line_start else 0)
                cur_start, cur_kind = line_start, "table"
        elif stripped.startswith("#"):
            close(line_start - 1 if line_start else 0)
            cur_start, cur_kind = line_start, "heading"
            close(line_end)
        else:
            if cur_kind in ("table", "heading"):
                close(line_start - 1 if line_start else 0)
            if cur_start is None:
                cur_start, cur_kind = line_start, "paragraph"
        pos = line_end + 1
    close(len(md))
    return blocks


def block_text(md: str, block: Block) -> str:
    return md[block.start:block.end]


def build_model_input(md: str, blocks: list[Block]) -> str:
    parts: list[str] = []
    for b in blocks:
        parts.append(f"[[{b.block_id}]]\n{md[b.start:b.end]}")
    return "\n\n".join(parts) + ("\n" if parts else "")


@dataclass
class QuoteCheck:
    status: str  # verified | verified_elsewhere | not_found | unverifiable | no_quote
    matched_block_id: str | None = None
    matched_page: int | None = None
    normalization: str = WS_NORMALIZATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "matched_block_id": self.matched_block_id, "matched_page": self.matched_page,
                "normalization": self.normalization}


@dataclass
class VerificationSources:
    markdown: str | None = None
    blocks: list[Block] = field(default_factory=list)
    pages: list[str] | None = None  # independent PDF text layer, 1-based pages at index i -> page i+1
    full_text: str | None = None  # DOCX text when no markdown

    @property
    def available(self) -> bool:
        return bool(self.markdown or self.pages or self.full_text)


def verify_quote(ev: Evidence, sources: VerificationSources) -> QuoteCheck:
    if not ev.quote or not ev.quote.strip():
        return QuoteCheck(status="no_quote")
    if not sources.available:
        return QuoteCheck(status="unverifiable")
    q = normalize_ws(ev.quote)
    if len(q) < 3:
        return QuoteCheck(status="unverifiable")
    if sources.markdown is not None and sources.blocks:
        if ev.block_id:
            for b in sources.blocks:
                if b.block_id == ev.block_id:
                    if q in normalize_ws(block_text(sources.markdown, b)):
                        return QuoteCheck(status="verified", matched_block_id=b.block_id)
                    break
        for b in sources.blocks:
            if q in normalize_ws(block_text(sources.markdown, b)):
                return QuoteCheck(status="verified_elsewhere" if ev.block_id else "verified", matched_block_id=b.block_id)
    elif sources.markdown is not None and q in normalize_ws(sources.markdown):
        return QuoteCheck(status="verified")
    if sources.pages:
        if ev.page and 1 <= ev.page <= len(sources.pages) and q in normalize_ws(sources.pages[ev.page - 1]):
            return QuoteCheck(status="verified", matched_page=ev.page)
        for i, p in enumerate(sources.pages):
            if q in normalize_ws(p):
                return QuoteCheck(status="verified_elsewhere" if ev.page else "verified", matched_page=i + 1)
    if sources.full_text and q in normalize_ws(sources.full_text):
        return QuoteCheck(status="verified")
    return QuoteCheck(status="not_found")


def _iter_envelopes(result: SdsExtraction):
    d = result.document
    yield "document.product_name", d.product_name
    yield "document.revision_date", d.revision_date
    yield "document.supplier_name", d.supplier_name
    for i, ing in enumerate(result.ingredients):
        yield f"ingredients[{i}].name", ing.name
        yield f"ingredients[{i}].cas_numbers", ing.cas_numbers
        yield f"ingredients[{i}].concentration", ing.concentration
    h = result.hazards
    yield "hazards.classifications", h.classifications
    yield "hazards.signal_word", h.signal_word
    yield "hazards.h_codes", h.h_codes
    yield "hazards.p_codes", h.p_codes
    yield "physical.flash_point", result.physical.flash_point


def verify_evidence(result: SdsExtraction, sources: VerificationSources) -> tuple[list[dict[str, Any]], list[Finding]]:
    """Verify every quote; returns (evidence_checks, findings). The result itself is left untouched."""
    checks: list[dict[str, Any]] = []
    findings: list[Finding] = []
    for path, env in _iter_envelopes(result):
        for j, ev in enumerate(env.evidence):
            qc = verify_quote(ev, sources)
            checks.append({"path": f"{path}.evidence[{j}]", **qc.to_dict()})
            if qc.status == "not_found":
                findings.append(Finding("quote_not_found", "error", path, "模型引用的原文在可驗證來源中找不到", data={"quote": ev.quote}))
            elif qc.status == "unverifiable":
                findings.append(Finding("quote_unverified", "info", path, "沒有可驗證來源，引用標記為 unverified", review_required=False))
            elif qc.status == "verified_elsewhere":
                findings.append(Finding("quote_location_mismatch", "warning", path, "引用存在，但不在模型所指的區塊／頁面",
                                        data={"claimed_block": ev.block_id, "claimed_page": ev.page,
                                              "found_block": qc.matched_block_id, "found_page": qc.matched_page}))
        if env.status == "found" and env.evidence and all(not (e.quote or "").strip() for e in env.evidence):
            findings.append(Finding("evidence_without_quote", "warning", path, "有 evidence 但沒有可核對的 quote"))
    return checks, findings
