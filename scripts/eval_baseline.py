#!/usr/bin/env python3
"""Minimal baseline check: match known expected changes to existing pipeline
findings with deterministic text anchors. Reads existing output only — no
pipeline or LLM run.

  python scripts/eval_baseline.py [--queue PATH] [--cases PATH] [--out PATH] [--csv] [--force]

result per case:
  TP         matched a finding classified substantive
  uncertain  matched a finding classified uncertain
  FN         no matching finding, or the match was classified non_substantive
"""
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUEUE = ROOT / "data" / "review" / "final_review_queue.json"
DEFAULT_CASES = ROOT / "data" / "eval" / "expected_cases.json"
DEFAULT_OUT = ROOT / "data" / "eval" / "results" / "baseline_before_tuning.json"

_RESULT_BY_CLASSIFICATION = {"substantive": "TP", "uncertain": "uncertain", "non_substantive": "FN"}
# Prefer the strongest classification when several findings match one case.
_PREFERENCE = {"substantive": 0, "uncertain": 1, "non_substantive": 2}


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").lower()


def matches(case: dict, finding: dict) -> bool:
    if finding["document_id"] != case["document_id"]:
        return False
    before, after = _norm(finding.get("before_text")), _norm(finding.get("after_text"))
    return (
        all(p in before for p in case.get("before_has", []))
        and all(p in after for p in case.get("after_has", []))
        and not any(p in before for p in case.get("before_lacks", []))
        and not any(p in after for p in case.get("after_lacks", []))
    )


def evaluate(findings: list[dict], cases: list[dict]) -> dict:
    rows, matched_ids = [], set()
    for case in cases:
        hits = sorted(
            (f for f in findings if matches(case, f)),
            key=lambda f: _PREFERENCE.get(f["classification"], 3),
        )
        best = hits[0] if hits else None
        if best:
            matched_ids.update(f["change_id"] for f in hits)
        rows.append(
            {
                "case_id": case["case_id"],
                "matched_change_id": best["change_id"] if best else None,
                "classification": best["classification"] if best else None,
                "revision_history_match": best["revision_history_match"] if best else None,
                "result": _RESULT_BY_CLASSIFICATION.get(best["classification"], "FN") if best else "FN",
                "all_matching_change_ids": [f["change_id"] for f in hits],
            }
        )

    in_scope = {c["document_id"] for c in cases}
    unmatched = [
        {"change_id": f["change_id"], "classification": f["classification"], "summary": f.get("summary")}
        for f in findings
        if f["document_id"] in in_scope
        and f["classification"] in ("substantive", "uncertain")
        and f["change_id"] not in matched_ids
    ]
    counts = {k: sum(1 for r in rows if r["result"] == k) for k in ("TP", "uncertain", "FN")}
    return {"summary": {"cases": len(rows), **counts}, "cases": rows, "potential_false_positives": unmatched}


CSV_FIELDS = [
    "case_id", "document_id", "ground_truth", "matched_change_id", "all_matching_change_ids",
    "classification", "revision_history_match", "result", "section", "summary", "before_text", "after_text",
]


def write_csv(result: dict, cases: list[dict], findings: list[dict], path: Path) -> None:
    """One row per case, plus the matched passage so a person can check the match."""
    cases_by_id = {c["case_id"]: c for c in cases}
    findings_by_id = {f["change_id"]: f for f in findings}
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in result["cases"]:
            case = cases_by_id[row["case_id"]]
            finding = findings_by_id.get(row["matched_change_id"], {})
            writer.writerow({
                **{k: row[k] for k in ("case_id", "matched_change_id", "classification", "revision_history_match", "result")},
                "document_id": case["document_id"],
                "ground_truth": case.get("ground_truth"),
                "all_matching_change_ids": "; ".join(row["all_matching_change_ids"]),
                "section": finding.get("section"),
                "summary": finding.get("summary"),
                "before_text": " ".join((finding.get("before_text") or "").split()),
                "after_text": " ".join((finding.get("after_text") or "").split()),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--csv", action="store_true", help="also write the case table as CSV next to --out")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = parser.parse_args()

    csv_path = args.out.with_suffix(".csv")
    for path in [args.out] + ([csv_path] if args.csv else []):
        if path.exists() and not args.force:
            print(f"REFUSING to overwrite {path} (a saved result); use --out or --force", file=sys.stderr)
            return 1

    findings = json.loads(args.queue.read_text())
    cases = json.loads(args.cases.read_text())["cases"]
    result = evaluate(findings, cases)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queue": str(args.queue.resolve().relative_to(ROOT)) if args.queue.resolve().is_relative_to(ROOT) else str(args.queue),
        "cases_file": str(args.cases.resolve().relative_to(ROOT)) if args.cases.resolve().is_relative_to(ROOT) else str(args.cases),
        **result,
    }

    print(f"{'case_id':40s} {'matched change_id':30s} {'classification':15s} {'rev_hist':8s} result")
    for r in result["cases"]:
        print(
            f"{r['case_id']:40s} {str(r['matched_change_id']):30s} {str(r['classification']):15s} "
            f"{str(r['revision_history_match']):8s} {r['result']}"
        )
    s = result["summary"]
    print(f"\n{s['TP']}/{s['cases']} TP · {s['uncertain']} uncertain · {s['FN']} FN")
    print(f"potential false positives (unmatched substantive/uncertain): {len(result['potential_false_positives'])}")
    for fp in result["potential_false_positives"]:
        print(f"  {fp['change_id']} ({fp['classification']}): {fp['summary']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"\nsaved {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    if args.csv:
        write_csv(result, cases, findings, csv_path)
        print(f"saved {csv_path.relative_to(ROOT) if csv_path.is_relative_to(ROOT) else csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
