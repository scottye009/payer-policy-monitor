#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from payer_policy.change import detect_changes
from payer_policy.models import ChangeRecord, PolicyDocument

PROCESSED_DIR = ROOT / "data" / "processed"
PRIOR_DIR = ROOT / "data" / "simulated_prior_processed"
OUTPUT_DIR = ROOT / "data" / "change_results"

# (prior document id, current document id) -- both read from their already
# processed JSON; no PDF is reopened.
PAIRS = [
    ("mri_ct_prior", "mri_ct_site_of_service"),
    ("spinraza_prior", "spinraza"),
    ("sleep_studies_prior", "sleep_studies"),
]

_CLASSIFICATION_PRIORITY = {"substantive": 0, "uncertain": 0, "non_substantive": 1}


def _load(path: Path) -> PolicyDocument:
    return PolicyDocument.model_validate_json(path.read_text())


def _review_queue_sort_key(record: ChangeRecord) -> tuple:
    return (_CLASSIFICATION_PRIORITY.get(record.classification, 0), -record.confidence)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_records: list[ChangeRecord] = []

    for prior_id, current_id in PAIRS:
        prior = _load(PRIOR_DIR / f"{prior_id}.json")
        current = _load(PROCESSED_DIR / f"{current_id}.json")

        print(f"Detecting changes: {prior_id} -> {current_id}")
        records = detect_changes(prior, current)
        all_records.extend(records)

        out_path = OUTPUT_DIR / f"{current_id}_changes.json"
        out_path.write_text(json.dumps([r.model_dump() for r in records], indent=2))
        print(f"OK      {len(records)} candidate(s) -> {out_path.relative_to(ROOT)}")

    all_records.sort(key=_review_queue_sort_key)
    queue_path = OUTPUT_DIR / "review_queue.json"
    queue_path.write_text(json.dumps([r.model_dump() for r in all_records], indent=2))
    print(f"OK      combined review queue ({len(all_records)} total) -> {queue_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
