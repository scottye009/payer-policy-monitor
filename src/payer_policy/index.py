import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from payer_policy.dates import parse_date

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
_MAX_ANCESTOR_LEVELS = 4
_LAST_PUBLISHED_RE = re.compile(r"Last Published\s*:?\s*([\d./]+)", re.IGNORECASE)


class IndexFetchError(Exception):
    """Raised when an index page cannot be retrieved."""


def fetch_index_html(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise IndexFetchError(f"request failed for {url}: {exc}") from exc

    if response.status_code != 200:
        raise IndexFetchError(f"non-200 response ({response.status_code}) for {url}")

    return response.text


def _pdf_path(url: str) -> str:
    return urlparse(url).path.rstrip("/").lower()


def _find_link(soup: BeautifulSoup, pdf_url: str):
    target = _pdf_path(pdf_url)
    for a in soup.find_all("a", href=True):
        if _pdf_path(a["href"]) == target:
            return a
    return None


def _find_last_published_text(link) -> str | None:
    """Walk a bounded number of ancestors looking for the "Last Published"
    element (marked with class "list-date" on UHC's index pages).

    Bounded so a shared list/grid ancestor holding many other documents'
    dates is never reached -- if the date isn't found within a few levels
    of the matched link, we give up rather than risk grabbing a
    neighboring document's date.
    """
    ancestor = link
    for _ in range(_MAX_ANCESTOR_LEVELS):
        ancestor = ancestor.parent
        if ancestor is None or ancestor.name in (None, "body", "html"):
            return None
        date_el = ancestor.find(class_="list-date")
        if date_el is not None:
            return date_el.get_text(" ", strip=True)
    return None


def find_last_published_date(html: str, pdf_url: str, document_id: str) -> str | None:
    """Locate the configured document on its index page by matching its PDF
    href, then extract the "Last Published" date next to it.

    Returns None and logs a warning if the document isn't listed on the
    index, or if no "Last Published" date can be found and parsed next to
    it -- the date is never guessed or backfilled from other page text.
    """
    soup = BeautifulSoup(html, "html.parser")
    link = _find_link(soup, pdf_url)
    if link is None:
        logger.warning("document %s: no matching link found on index page for %s", document_id, pdf_url)
        return None

    date_text = _find_last_published_text(link)
    if date_text is None:
        logger.warning(
            "document %s: matched index link but found no adjacent 'Last Published' date", document_id
        )
        return None

    match = _LAST_PUBLISHED_RE.search(date_text)
    if not match:
        logger.warning("document %s: could not find a date in index text %r", document_id, date_text)
        return None

    parsed = parse_date(match.group(1))
    if parsed is None:
        logger.warning(
            "document %s: found 'Last Published' text %r but could not parse a date from it",
            document_id,
            match.group(1),
        )
    return parsed
