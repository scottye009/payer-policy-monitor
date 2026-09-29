#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from payer_policy.collect import sha256_hex
from payer_policy.extract import extract_metadata, extract_pages
from payer_policy.models import PolicyDocument

RAW_DIR = ROOT / "data" / "simulated_prior_raw"
PROCESSED_DIR = ROOT / "data" / "simulated_prior_processed"

PAYER = "UnitedHealthcare"

# document_type is manually curated here just as it is in config/sources.yaml
# for the corresponding current-version documents; title is not -- it's
# extracted from the PDF's own text below, same as any other document.
SIMULATED_PRIORS = [
    {"filename": "mri_ct_prior.pdf", "document_type": "medical_policy"},
    {"filename": "spinraza_prior.pdf", "document_type": "medical_benefit_drug_policy"},
    {"filename": "sleep_studies_prior.pdf", "document_type": "medical_policy"},
]


def _first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def process_one(entry: dict) -> PolicyDocument:
    pdf_path = RAW_DIR / entry["filename"]
    content = pdf_path.read_bytes()
    content_hash = sha256_hex(content)

    pages = extract_pages(content)
    metadata = extract_metadata(content, pages)

    title = _first_nonempty_line(pages[0].text) if pages else None
    if title is None:
        raise ValueError(f"{entry['filename']}: could not determine a title from page 1 text")

    relative_path = str(pdf_path.relative_to(ROOT))

    return PolicyDocument(
        document_id=pdf_path.stem,
        title=title,
        payer=PAYER,
        document_type=entry["document_type"],
        source_url=None,
        publication_date=metadata.publication_date,
        revision_date=metadata.revision_date,
        effective_date=metadata.effective_date,
        policy_number=metadata.policy_number,
        retrieved_at=None,
        content_sha256=content_hash,
        local_path=relative_path,
        pages=pages,
        is_simulated=True,
        artifact_type="simulated_prior",
        source_path=relative_path,
    )


def main() -> int:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    for entry in SIMULATED_PRIORS:
        document = process_one(entry)
        out_path = PROCESSED_DIR / f"{document.document_id}.json"
        out_path.write_text(document.model_dump_json(indent=2))
        print(f"OK      {document.document_id} -> {out_path.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
