"""Evaluation CLI (runs locally; imports the backend package).

Commands
  manifest            Scan TEST_SDS_DIR: format, size, pages, text-layer chars, processability -> manifest.json
  inspect-conversion  Run MarkItDown on every processable document and write markdown + block maps for human review
  gold-template       Create an EMPTY gold skeleton per document (sha256-keyed) for a human reviewer to fill
  submit              Upload every manifest document to the running API for the given arms (real cost needs --confirm-real-cost)
  score               Score exported results (experiment export JSON) against finished gold files
  summary             Print an experiment summary from the running API

Nothing here generates sample documents or gold values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from domain.errors import PermanentError  # noqa: E402
from domain.extraction_schema import SCHEMA_VERSION, SET_FIELD_PATHS, SdsExtraction  # noqa: E402
from services.document_check import check_document, sniff_format  # noqa: E402
from services.gold import load_gold_by_sha  # noqa: E402
from services.scoring import score_extraction  # noqa: E402

DEFAULT_OUT = ROOT / "evaluation" / "output"
DEFAULT_GOLD = ROOT / "data" / "gold"
SCALAR_GOLD_FIELDS = ["document.product_name", "document.revision_date", "document.supplier_name", "hazards.signal_word", "physical.flash_point"]


def _docs(test_dir: str | None) -> list[Path]:
    if not test_dir:
        sys.exit("TEST_SDS_DIR is not set (or pass --dir). No sample documents are generated; point this at your own PDF/DOCX files.")
    p = Path(test_dir)
    if not p.is_dir():
        sys.exit(f"TEST_SDS_DIR {p} is not a directory")
    files = sorted(f for f in p.rglob("*") if f.suffix.lower() in (".pdf", ".docx", ".doc"))
    if not files:
        sys.exit(f"No PDF/DOCX files under {p}")
    return files


def cmd_manifest(args) -> None:
    rows = []
    for f in _docs(args.dir):
        data = f.read_bytes()
        row = {"file": str(f.relative_to(args.dir)), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
               "split": args.split}
        try:
            fmt = sniff_format(data, f.name, None)
            check = check_document(data, fmt, min_text_chars_per_page=args.min_chars)
            row.update({"format": fmt.value, "classification": check.classification, "page_count": check.page_count,
                        "pages_text_chars": check.pages_text_chars, "total_text_chars": check.total_text_chars, "warnings": check.warnings})
        except PermanentError as e:
            row.update({"format": None, "classification": e.code, "error": e.message})
        rows.append(row)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "source_dir": str(args.dir), "min_text_chars_per_page": args.min_chars,
                "documents": rows}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
    print(f"{len(rows)} documents -> {out / 'manifest.json'}")
    for k, v in sorted(by_class.items()):
        print(f"  {k}: {v}")


def cmd_inspect(args) -> None:
    from adapters.markitdown_adapter import MarkItDownConverter
    from services.locator import build_model_input, segment_markdown

    conv = MarkItDownConverter()
    out = Path(args.out) / "conversions"
    out.mkdir(parents=True, exist_ok=True)
    report = []
    for f in _docs(args.dir):
        data = f.read_bytes()
        try:
            fmt = sniff_format(data, f.name, None)
            check = check_document(data, fmt, min_text_chars_per_page=args.min_chars)
        except PermanentError as e:
            report.append({"file": f.name, "skipped": e.code})
            continue
        if check.classification == "needs_ocr":
            report.append({"file": f.name, "skipped": "needs_ocr"})
            continue
        t0 = time.monotonic()
        res = conv.convert(data, fmt, f.name)
        blocks = segment_markdown(res.markdown)
        stem = hashlib.sha256(data).hexdigest()[:16]
        (out / f"{stem}.md").write_text(res.markdown, encoding="utf-8")
        (out / f"{stem}.model-input.txt").write_text(build_model_input(res.markdown, blocks), encoding="utf-8")
        (out / f"{stem}.blocks.json").write_text(json.dumps([b.to_dict() for b in blocks], indent=2), encoding="utf-8")
        report.append({"file": f.name, "sha256_prefix": stem, "format": fmt.value, "classification": check.classification,
                       "markdown_chars": len(res.markdown), "blocks": len(blocks), "tables": sum(1 for b in blocks if b.kind == "table"),
                       "convert_ms": int((time.monotonic() - t0) * 1000), "converter": f"{res.converter} {res.converter_version}",
                       "warnings": res.warnings})
        print(json.dumps(report[-1], ensure_ascii=False))
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(report)} entries to {out}. Review tables, symbols and raw text by hand before building gold.")


def cmd_gold_template(args) -> None:
    gold_dir = Path(args.gold_dir)
    gold_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    for f in _docs(args.dir):
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        target = gold_dir / f"{sha}.json"
        if target.exists():
            continue
        skeleton = {
            "_instructions": "Fill by reading the ORIGINAL document. Leave reviewed_at empty until finished; set status to 'final' when done. "
                             "Use status not_reported/no_data/not_applicable when the document says so. Do not copy model output blindly.",
            "document_sha256": sha, "file_name": f.name, "schema_version": SCHEMA_VERSION, "status": "draft", "reviewer": "", "reviewed_at": "",
            "fields": {p: {"status": "", "value": None, "accepted_values": []} for p in SCALAR_GOLD_FIELDS},
            "sets": {p: [] for p in SET_FIELD_PATHS},
            "ingredients": [{"name": "", "accepted_names": [], "cas": [], "concentration": {"comparator": "", "min": None, "max": None, "unit": "", "basis": None}}],
            "notes": "",
        }
        target.write_text(json.dumps(skeleton, ensure_ascii=False, indent=2), encoding="utf-8")
        created += 1
    print(f"{created} gold skeleton(s) created in {gold_dir} (existing files untouched). They are git-ignored.")


def cmd_submit(args) -> None:
    import httpx

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    base = args.api.rstrip("/")
    health = httpx.get(f"{base}/health", timeout=30).json()
    if not health.get("is_test_double") and not args.confirm_real_cost:
        sys.exit("API is configured with a REAL provider. Re-run with --confirm-real-cost to spend money on this batch.")
    arms = [a.strip().upper() for a in args.arms.split(",")]
    results = []
    for row in manifest["documents"]:
        if row.get("classification") in ("unsupported_format", "encrypted_document", "corrupt_document", "document_too_large"):
            results.append({**row, "submitted": False, "reason": row["classification"]})
            continue
        path = Path(manifest["source_dir"]) / row["file"]
        with path.open("rb") as fh:
            r = httpx.post(f"{base}/jobs", files={"file": (path.name, fh)}, data={"arms": ",".join(arms), "experiment_id": args.experiment},
                           headers={"Idempotency-Key": f"{args.experiment}:{row['sha256']}:{','.join(sorted(arms))}"}, timeout=120)
        results.append({**row, "submitted": r.status_code in (202, 503), "http_status": r.status_code, "response": r.json()})
        print(path.name, r.status_code)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"submit-{args.experiment}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_score(args) -> None:
    gold = load_gold_by_sha(args.gold_dir)
    export = json.loads(Path(args.export).read_text(encoding="utf-8"))
    rows = export["rows"]
    per_arm: dict[str, list[dict]] = {}
    scored = 0
    results_dir = Path(args.results_dir) if args.results_dir else None
    for row in rows:
        sha = row.get("document_sha256")
        if sha not in gold or row.get("is_test_double") or row.get("status") not in ("succeeded", "needs_review"):
            continue
        if not results_dir:
            continue
        rp = results_dir / f"{row['job_id']}-{row['run_id']}-result.json"
        if not rp.exists():
            continue
        extraction = SdsExtraction.model_validate(json.loads(rp.read_text(encoding="utf-8"))["extraction"])
        s = score_extraction(extraction, gold[sha])
        per_arm.setdefault(row["arm"], []).append(s["metrics"])
        scored += 1
    print(f"scored {scored} result(s) against {len(gold)} finished gold file(s)")
    for arm, ms in sorted(per_arm.items()):
        vc = sum(m["valued_correct"] for m in ms)
        vt = sum(m["valued_total"] for m in ms)
        rc = sum(m["ingredient_rows_correct"] for m in ms)
        rg = sum(m["ingredient_rows_gold"] for m in ms)
        print(f"  arm {arm}: valued fields {vc}/{vt}" + (f" = {vc / vt:.1%}" if vt else "") + f"; ingredient rows {rc}/{rg}; n={len(ms)}")
    if not scored:
        print("No scored documents: accuracy is UNVERIFIED. Finish gold files (status=final, reviewed_at set) and export results first.")


def cmd_summary(args) -> None:
    import httpx

    r = httpx.get(f"{args.api.rstrip('/')}/experiments/{args.experiment}/summary", timeout=60)
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=os.environ.get("TEST_SDS_DIR"), help="directory with the user's PDF/DOCX (default TEST_SDS_DIR)")
    common.add_argument("--out", default=str(DEFAULT_OUT))
    common.add_argument("--min-chars", type=int, default=int(os.environ.get("MIN_TEXT_CHARS_PER_PAGE", "40")))

    s = sub.add_parser("manifest", parents=[common])
    s.add_argument("--split", default="dev", help="dev | eval (keep tuning and evaluation documents apart)")
    s.set_defaults(fn=cmd_manifest)
    s = sub.add_parser("inspect-conversion", parents=[common])
    s.set_defaults(fn=cmd_inspect)
    s = sub.add_parser("gold-template", parents=[common])
    s.add_argument("--gold-dir", default=str(DEFAULT_GOLD))
    s.set_defaults(fn=cmd_gold_template)
    s = sub.add_parser("submit")
    s.add_argument("--manifest", default=str(DEFAULT_OUT / "manifest.json"))
    s.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:7071/api"))
    s.add_argument("--arms", default="A,B,C,D")
    s.add_argument("--experiment", required=True)
    s.add_argument("--confirm-real-cost", action="store_true")
    s.add_argument("--out", default=str(DEFAULT_OUT))
    s.set_defaults(fn=cmd_submit)
    s = sub.add_parser("score")
    s.add_argument("--export", required=True, help="JSON from GET /experiments/{id}/export?format=json")
    s.add_argument("--results-dir", help="directory with downloaded result artifacts named {job_id}-{run_id}-result.json")
    s.add_argument("--gold-dir", default=str(DEFAULT_GOLD))
    s.set_defaults(fn=cmd_score)
    s = sub.add_parser("summary")
    s.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:7071/api"))
    s.add_argument("--experiment", required=True)
    s.set_defaults(fn=cmd_summary)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
