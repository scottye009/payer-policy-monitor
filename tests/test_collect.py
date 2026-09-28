import hashlib
from unittest.mock import Mock, patch

import pymupdf as fitz
import pytest

from payer_policy.collect import CollectionError, fetch_pdf, sha256_hex
from payer_policy.extract import extract_metadata, extract_pages


def test_sha256_hex_known_bytes():
    data = b"hello world"
    assert sha256_hex(data) == hashlib.sha256(data).hexdigest()


def _build_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_extract_pages_and_effective_date():
    pdf_bytes = _build_pdf("Effective Date: January 15, 2026")
    pages = extract_pages(pdf_bytes)

    assert len(pages) == 1
    assert pages[0].page_number == 1
    assert "Effective Date: January 15, 2026" in pages[0].text

    metadata = extract_metadata(pages)
    assert metadata.effective_date == "2026-01-15"
    assert metadata.publication_date is None
    assert metadata.revision_date is None
    assert metadata.policy_number is None


def test_extract_metadata_missing_date_stays_none():
    pdf_bytes = _build_pdf("This policy has a revision history section but no dated label.")
    pages = extract_pages(pdf_bytes)

    metadata = extract_metadata(pages)
    assert metadata.effective_date is None
    assert metadata.publication_date is None
    assert metadata.revision_date is None
    assert metadata.policy_number is None


def test_policy_number_without_colon():
    pdf_bytes = _build_pdf("Policy Number 2026R8021A")
    pages = extract_pages(pdf_bytes)

    metadata = extract_metadata(pages)
    assert metadata.policy_number == "2026R8021A"


def test_publication_date_from_policy_published_history_line():
    pdf_bytes = _build_pdf(
        "History\n"
        "9/1/2026\n"
        "Policy implemented by UnitedHealthcare\n"
        "6/1/2026\n"
        "Policy published\n"
        "4/28/2026\n"
        "Policy approved by the Reimbursement Policy Oversight Committee"
    )
    pages = extract_pages(pdf_bytes)

    metadata = extract_metadata(pages)
    assert metadata.publication_date == "2026-06-01"
    # Neighboring history dates (implementation, approval) must not leak in.
    assert metadata.revision_date is None


def test_revision_date_from_policy_history_section_ignores_toc_and_uses_latest():
    pdf_bytes = _build_pdf(
        "Table of Contents\n"
        "Policy History/Revision Information .......... 7\n"
        "References\n"
        "Some Author. Journal. 2024;14(4).\n"
        "Policy History/Revision Information\n"
        "Date\n"
        "Summary of Changes\n"
        "03/01/2025\n"
        "Earlier revision notes\n"
        "09/01/2026\n"
        "Latest revision notes\n"
        "Instructions for Use\n"
        "This policy provides assistance in interpreting benefit plans."
    )
    pages = extract_pages(pdf_bytes)

    metadata = extract_metadata(pages)
    assert metadata.revision_date == "2026-09-01"


def test_revision_date_none_when_no_history_section():
    pdf_bytes = _build_pdf("Effective Date: January 1, 2026\nNo revision history section here.")
    pages = extract_pages(pdf_bytes)

    metadata = extract_metadata(pages)
    assert metadata.revision_date is None


def test_fetch_pdf_raises_on_non_200():
    mock_response = Mock(status_code=404, content=b"not found")
    with patch("payer_policy.collect.requests.get", return_value=mock_response) as mock_get:
        with pytest.raises(CollectionError):
            fetch_pdf("https://example.com/fake.pdf")
        mock_get.assert_called_once()


def test_fetch_pdf_raises_on_non_pdf_content():
    mock_response = Mock(status_code=200, content=b"<html>not a pdf</html>")
    with patch("payer_policy.collect.requests.get", return_value=mock_response):
        with pytest.raises(CollectionError):
            fetch_pdf("https://example.com/fake.pdf")
