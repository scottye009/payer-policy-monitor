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
    # Matches the date shape itself rather than reading to end-of-line: with
    # sort=True extraction, unrelated same-row content (e.g. a right-aligned
    # "Instructions for Use" page marker) can land on the same text line as
    # this label, so there's no reliable line ending to anchor on.
    "effective_date": re.compile(
        r"Effective Date:\s*([A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})", re.IGNORECASE
    ),
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
    """Extract page text in spatial (visual) reading order rather than PDF
    object insertion order. Edited/reinserted text objects -- as in the
    simulated prior PDFs -- can otherwise land at the end of a page's text
    instead of their visual position, fragmenting paragraphs. This is the
    single extraction path shared by real and simulated PDFs."""
    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc):
            pages.append(PageText(page_number=index + 1, text=page.get_text("text", sort=True)))
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


def find_revision_section_span(full_text: str) -> tuple[int, int] | None:
    """Return the (start, end) character offsets of the Policy
    History/Revision Information section's body within `full_text` (pages
    joined with "\n"), or None if the heading isn't present.

    The heading also appears in the table of contents (followed by dot
    leaders and a page number); the real section is always the last
    occurrence in reading order. Reused by change.py to exclude this
    section from the prior/current content diff.
    """
    starts = list(_REVISION_SECTION_START_RE.finditer(full_text))
    if not starts:
        return None
    start = starts[-1].end()
    end_match = _REVISION_SECTION_END_RE.search(full_text, start)
    end = end_match.start() if end_match else len(full_text)
    return start, end


def _revision_section_text(full_text: str) -> str | None:
    span = find_revision_section_span(full_text)
    if span is None:
        return None
    start, end = span
    return full_text[start:end]


def _position_sorted_page_text(page: "fitz.Page") -> str:
    """Reconstruct a page's text ordered strictly by block position
    (top-to-bottom, then left-to-right) instead of PyMuPDF's default
    reading-order heuristic.

    That heuristic can relocate a short, narrow table cell -- e.g. a
    one-line "Date" column value next to a much taller "Summary of
    Changes" cell in the same row -- arbitrarily far down the page, past
    unrelated boilerplate text and beyond where a bounded section scan
    would look for it.
    """
    blocks = page.get_text("blocks")
    blocks = sorted(blocks, key=lambda b: (b[1], b[0]))
    return "\n".join(block[4] for block in blocks)


def _find_revision_date(pdf_bytes: bytes) -> str | None:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        full_text = "\n".join(_position_sorted_page_text(page) for page in doc)

    section = _revision_section_text(full_text)
    if section is None:
        return None

    parsed_dates = [parse_date(token) for token in _DATE_TOKEN_RE.findall(section)]
    valid_dates = [d for d in parsed_dates if d is not None]
    if not valid_dates:
        return None
    return max(valid_dates)


def extract_metadata(pdf_bytes: bytes, pages: list[PageText]) -> ExtractedMetadata:
    policy_number = _find_labeled_value(_LABEL_PATTERNS["policy_number"], pages)
    effective_raw = _find_labeled_value(_LABEL_PATTERNS["effective_date"], pages)

    return ExtractedMetadata(
        policy_number=policy_number,
        effective_date=parse_date(effective_raw) if effective_raw else None,
        publication_date=_find_publication_date(pages),
        revision_date=_find_revision_date(pdf_bytes),
    )
