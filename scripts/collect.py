#!/usr/bin/env python3
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from payer_policy.bulletin import find_bulletin_effective_date
from payer_policy.collect import CollectionError, fetch_pdf, save_raw_pdf, sha256_hex
from payer_policy.extract import extract_metadata, extract_pages
from payer_policy.index import IndexFetchError, fetch_index_html, find_last_published_date
from payer_policy.models import (
    EffectiveDateProvenance,
    ExtractedMetadata,
    IndexSource,
    PageText,
    PolicyDocument,
    SourceConfig,
)

CONFIG_PATH = ROOT / "config" / "sources.yaml"
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"

logger = logging.getLogger(__name__)


@dataclass
class FetchedDocument:
    content_hash: str
    local_path: Path
    pages: list[PageText]
    metadata: ExtractedMetadata


def load_sources(config_path: Path) -> tuple[str, str | None, dict[str, IndexSource], list[SourceConfig]]:
    with config_path.open() as f:
        raw = yaml.safe_load(f)
    payer = raw["payer"]
    geography = raw.get("organization", {}).get("geography")
    indexes = {name: IndexSource(**cfg) for name, cfg in raw.get("indexes", {}).items()}
    sources = [SourceConfig(**item) for item in raw["documents"]]
    return payer, geography, indexes, sources


def load_index_pages(indexes: dict[str, IndexSource]) -> dict[str, tuple[str, str, datetime]]:
    """Fetch each configured index page once, up front.

    Returns name -> (url, html, observed_at). An index that fails to fetch
    is omitted; documents belonging to it fall back to null index fields
    with a warning rather than guessing or retrying per document.
    """
    pages: dict[str, tuple[str, str, datetime]] = {}
    for name, index_source in indexes.items():
        try:
            html = fetch_index_html(index_source.url)
        except IndexFetchError as exc:
            logger.warning("index '%s' (%s) could not be retrieved: %s", name, index_source.url, exc)
            continue
        pages[name] = (index_source.url, html, datetime.now(timezone.utc))
    return pages


def fetch_and_extract(source: SourceConfig) -> FetchedDocument:
    content = fetch_pdf(source.source_url)
    content_hash = sha256_hex(content)
    local_path = save_raw_pdf(RAW_DIR, source.id, content)

    pages = extract_pages(content)
    metadata = extract_metadata(content, pages)
    return FetchedDocument(content_hash, local_path, pages, metadata)


def resolve_effective_date(
    source: SourceConfig,
    metadata: ExtractedMetadata,
    geography: str | None,
    fetched: dict[str, FetchedDocument],
) -> tuple[str | None, EffectiveDateProvenance | None]:
    """Prefer the policy PDF's own Effective Date; only if it's absent, and
    a bulletin is configured, resolve a default/state-exception date from
    that bulletin for the configured organization geography."""
    if metadata.effective_date is not None:
        return metadata.effective_date, None

    if source.effective_date_bulletin is None or geography is None:
        return None, None

    bulletin = fetched.get(source.effective_date_bulletin)
    if bulletin is None:
        logger.warning(
            "document %s: bulletin '%s' unavailable, effective_date left null",
            source.id,
            source.effective_date_bulletin,
        )
        return None, None

    date, used_exception = find_bulletin_effective_date(bulletin.pages, source.title, geography)
    if date is None:
        logger.warning(
            "document %s: no entry for '%s' found in bulletin '%s'",
            source.id,
            source.title,
            source.effective_date_bulletin,
        )
        return None, None

    provenance = EffectiveDateProvenance(
        source="bulletin",
        bulletin_document_id=source.effective_date_bulletin,
        geography=geography,
        used_state_exception=used_exception,
    )
    return date, provenance


def build_document(
    payer: str,
    geography: str | None,
    source: SourceConfig,
    fetched: dict[str, FetchedDocument],
    index_pages: dict[str, tuple[str, str, datetime]],
) -> PolicyDocument:
    doc = fetched[source.id]
    metadata = doc.metadata

    effective_date, effective_date_provenance = resolve_effective_date(source, metadata, geography, fetched)

    last_published_date = None
    index_url = None
    index_observed_at = None

    if source.index is not None:
        page = index_pages.get(source.index)
        if page is None:
            logger.warning(
                "document %s: index '%s' page unavailable, last_published_date left null",
                source.id,
                source.index,
            )
        else:
            index_url, html, index_observed_at = page
            last_published_date = find_last_published_date(html, source.source_url, source.id)

    return PolicyDocument(
        document_id=source.id,
        title=source.title,
        payer=payer,
        document_type=source.document_type,
        source_url=source.source_url,
        publication_date=metadata.publication_date,
        revision_date=metadata.revision_date,
        effective_date=effective_date,
        effective_date_provenance=effective_date_provenance,
        policy_number=metadata.policy_number,
        last_published_date=last_published_date,
        index_url=index_url,
        index_observed_at=index_observed_at,
        retrieved_at=datetime.now(timezone.utc),
        content_sha256=doc.content_hash,
        local_path=str(doc.local_path.relative_to(ROOT)),
        pages=doc.pages,
    )


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="WARNING %(name)s: %(message)s")

    payer, geography, indexes, sources = load_sources(CONFIG_PATH)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    index_pages = load_index_pages(indexes)

    fetched: dict[str, FetchedDocument] = {}
    failures = []
    for source in sources:
        try:
            fetched[source.id] = fetch_and_extract(source)
        except CollectionError as exc:
            print(f"FAILED  {source.id}: {exc}", file=sys.stderr)
            failures.append(source.id)

    for source in sources:
        if source.id not in fetched:
            continue

        document = build_document(payer, geography, source, fetched, index_pages)
        out_path = PROCESSED_DIR / f"{source.id}.json"
        out_path.write_text(document.model_dump_json(indent=2))
        print(f"OK      {source.id} -> {out_path.relative_to(ROOT)}")

    if failures:
        print(f"\n{len(failures)} document(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
