import re

import pymupdf as fitz

from payer_policy.dates import parse_date
from payer_policy.models import ExtractedMetadata, PageText

# Each pattern matches only its own explicitly labeled field. There is no
# fallback between them, so e.g. an effective date is never used to fill in
# a missing publication or revision date.
_LABEL_PATTERNS = {
    # Some UHC templates omit the colon ("Policy Number 2026R8021A").
    "policy_number": re.compile(r"Policy Number:?\s+([A-Za-z0-9\-\.]+)", re.IGNORECASE),
    "effective_date": re.compile(r"Effective Date:\s*([A-Za-z0-9,/ ]+?)(?:\n|$)", re.IGNORECASE),
}

# "Policy published" history rows read as "<date>\nPolicy published" once
# extracted from the source table. This is distinct from the index page's
# "Last Published" date and is never derived from it.
_PUBLISHED_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s+Policy published", re.IGNORECASE)

# Bounds revision-date parsing to the formal revision history section only,
# so a date is never picked up from unrelated text elsewhere in the PDF.
_REVISION_SECTION_START_RE = re.compile(r"Policy History/Revision Information", re.IGNORECASE)
_REVISION_SECTION_END_RE = re.compile(r"Instructions for Use", re.IGNORECASE)
_DATE_TOKEN_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def extract_pages(pdf_bytes: bytes) -> list[PageText]:
    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            pages.append(PageText(page_number=index + 1, text=page.get_text()))
    return pages


def _find_labeled_value(pattern: re.Pattern, pages: list[PageText]) -> str | None:
    for page in pages:
        match = pattern.search(page.text)
        if match:
            return match.group(1).strip()
    return None


def _full_text(pages: list[PageText]) -> str:
    return "\n".join(page.text for page in pages)


def _find_publication_date(pages: list[PageText]) -> str | None:
    match = _PUBLISHED_RE.search(_full_text(pages))
    if match is None:
        return None
    return parse_date(match.group(1))


def _revision_section_text(full_text: str) -> str | None:
    # The heading also appears in the table of contents (followed by dot
    # leaders and a page number); the real section is always the last
    # occurrence in reading order.
    starts = list(_REVISION_SECTION_START_RE.finditer(full_text))
    if not starts:
        return None
    start = starts[-1].end()
    end_match = _REVISION_SECTION_END_RE.search(full_text, start)
    end = end_match.start() if end_match else len(full_text)
    return full_text[start:end]


def _find_revision_date(pages: list[PageText]) -> str | None:
    section = _revision_section_text(_full_text(pages))
    if section is None:
        return None

    parsed_dates = [parse_date(token) for token in _DATE_TOKEN_RE.findall(section)]
    valid_dates = [d for d in parsed_dates if d is not None]
    if not valid_dates:
        return None
    return max(valid_dates)


def extract_metadata(pages: list[PageText]) -> ExtractedMetadata:
    policy_number = _find_labeled_value(_LABEL_PATTERNS["policy_number"], pages)
    effective_raw = _find_labeled_value(_LABEL_PATTERNS["effective_date"], pages)

    return ExtractedMetadata(
        policy_number=policy_number,
        effective_date=parse_date(effective_raw) if effective_raw else None,
        publication_date=_find_publication_date(pages),
        revision_date=_find_revision_date(pages),
    )
