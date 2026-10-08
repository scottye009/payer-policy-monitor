#!/usr/bin/env python3
"""Orchestration glue between source monitoring and the existing pipeline:
an UPDATED source check -> extraction -> detect_changes() -> review
processing, for exactly that check's previous/current version pair.

Nothing here changes detection, alignment, adjudication, or relevance
logic, and nothing is written to data/raw, data/processed, or data/review.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collect import CONFIG_PATH, load_sources
from review_processing import CLAIM_VOLUME_PATH, PROFILE_PATH, load_claim_volume, load_profile, process_review_queue

from payer_policy.adjudicate import adjudicate
from payer_policy.change import detect_changes
from payer_policy.collect import sha256_hex
from payer_policy.extract import extract_metadata, extract_pages
from payer_policy.models import PolicyDocument, SourceConfig
from payer_policy.source_check import (
    DEFAULT_DB_PATH,
    UPDATED,
    CheckResult,
    SourceVersion,
    get_status,
    get_version_by_hash,
)


@dataclass
class VersionPairFindings:
    policy_id: str
    previous_hash: str
    current_hash: str
    previous_is_simulated: bool
    # Same record shape as data/review/final_review_queue.json.
    findings: list[dict]


def build_policy_document(source: SourceConfig, payer: str, version: SourceVersion) -> PolicyDocument:
    """Identity (id/title/type) comes from config for both simulated and live
    versions; only the version itself says whether it is simulated."""
    content = (ROOT / version.snapshot_path).read_bytes()
    if sha256_hex(content) != version.content_sha256:
        raise ValueError(f"snapshot {version.snapshot_path} does not match its recorded hash")

    pages = extract_pages(content)
    metadata = extract_metadata(content, pages)
    return PolicyDocument(
        document_id=source.id,
        title=source.title,
        payer=payer,
        document_type=source.document_type,
        source_url=version.source_url,
        publication_date=metadata.publication_date,
        revision_date=metadata.revision_date,
        effective_date=metadata.effective_date,
        policy_number=metadata.policy_number,
        # A simulated version was registered, never retrieved.
        retrieved_at=None if version.is_simulated else version.retrieved_at,
        content_sha256=version.content_sha256,
        local_path=version.snapshot_path,
        pages=pages,
        is_simulated=version.is_simulated,
        artifact_type="simulated_prior" if version.is_simulated else None,
        source_path=version.snapshot_path if version.is_simulated else None,
    )


def run_update_pipeline(
    policy_id: str,
    check_result: CheckResult | None,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    adjudicate_fn=adjudicate,
) -> VersionPairFindings | None:
    """Findings for an UPDATED check's version pair; None for any other status."""
    if check_result is None or check_result.status != UPDATED:
        return None

    versions = []
    for content_hash in (check_result.previous_hash, check_result.current_hash):
        version = get_version_by_hash(policy_id, content_hash, db_path=db_path)
        if version is None:
            raise LookupError(f"{policy_id}: no stored version with hash {content_hash}")
        versions.append(version)
    previous, current = versions

    payer, _geography, _indexes, sources = load_sources(CONFIG_PATH)
    source = next(s for s in sources if s.id == policy_id)
    prior_doc = build_policy_document(source, payer, previous)
    current_doc = build_policy_document(source, payer, current)

    records = detect_changes(prior_doc, current_doc, adjudicate_fn=adjudicate_fn)
    findings = process_review_queue(
        [r.model_dump() for r in records],
        load_profile(PROFILE_PATH),
        load_claim_volume(CLAIM_VOLUME_PATH),
    )
    return VersionPairFindings(
        policy_id=policy_id,
        previous_hash=previous.content_sha256,
        current_hash=current.content_sha256,
        previous_is_simulated=previous.is_simulated,
        findings=findings,
    )


def main() -> int:
    """Analyze the latest check for each policy id given on the command line."""
    for policy_id in sys.argv[1:]:
        result = run_update_pipeline(policy_id, get_status(policy_id))
        if result is None:
            print(f"SKIP    {policy_id}: latest check is not 'updated'")
            continue
        print(
            f"OK      {policy_id}: {len(result.findings)} finding(s) for "
            f"{result.previous_hash[:12]} -> {result.current_hash[:12]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
