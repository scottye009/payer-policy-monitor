import logging
from unittest.mock import Mock, patch

from payer_policy.index import fetch_index_html, find_last_published_date

SPINRAZA_URL = (
    "https://www.uhcprovider.com/content/dam/provider/docs/public/policies/"
    "comm-medical-drug/spinraza-nusinersen.pdf"
)

# Mirrors the real UHC index card structure: the "Last Published" date lives
# in its own p.list-date element, while an unrelated "Effective Date" sits in
# a sibling p.faceted-item-description -- included here to prove the two
# don't get conflated.
INDEX_HTML = """
<html><body>
<div class="faceted-list-item">
  <h5 class="list-title">
    <a href="/content/dam/provider/docs/public/policies/comm-medical-drug/spinraza-nusinersen.pdf" target="_blank">
      Spinraza (Nusinersen)
    </a>
  </h5>
  <div class="tag-and-date-wrapper">
    <p class="text-category-bullet list-date">Last Published 09.01.2026</p>
  </div>
  <p class="faceted-item-description">Effective Date: 09.01.2026 - policy text.</p>
</div>
</body></html>
"""


def test_find_last_published_date_success():
    mock_response = Mock(status_code=200, text=INDEX_HTML)
    with patch("payer_policy.index.requests.get", return_value=mock_response):
        html = fetch_index_html("https://example.com/index.html")

    date = find_last_published_date(html, SPINRAZA_URL, "spinraza")
    assert date == "2026-09-01"


def test_find_last_published_date_missing_entry(caplog):
    mock_response = Mock(status_code=200, text="<html><body><div>no matching link here</div></body></html>")
    with patch("payer_policy.index.requests.get", return_value=mock_response):
        html = fetch_index_html("https://example.com/index.html")

    with caplog.at_level(logging.WARNING):
        date = find_last_published_date(html, SPINRAZA_URL, "spinraza")

    assert date is None
    assert "no matching link" in caplog.text
