import re

from payer_policy.dates import parse_date
from payer_policy.models import PageText

# A bulletin entry's effective-date column reads as a default date optionally
# followed by one or more "<date> for <STATES>." exception clauses, e.g.
# "9/1/2026 10/1/2026 for AR, CO, KY, NC, NE, OH, and RI."
_CLAUSE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+for\s+([A-Z]{2}(?:,\s*[A-Z]{2})*(?:,?\s*and\s+[A-Z]{2})?)\.)?"
)
_WINDOW_CHARS = 300
# Marks where an entry's date column ends and its prose summary begins, so
# the clause scan never wanders into unrelated text.
_WINDOW_STOP_MARKERS = ("Effective for dates of service", "This new policy is available")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_state_list(raw: str) -> list[str]:
    raw = raw.replace(" and ", ", ")
    return [code.strip() for code in raw.split(",") if code.strip()]


def find_bulletin_effective_date(
    bulletin_pages: list[PageText], title: str, geography: str
) -> tuple[str | None, bool]:
    """Locate `title`'s entry in a UHC reimbursement bulletin's policy table
    and resolve its effective date for the given organization geography.

    Returns (date, used_state_exception). Returns (None, False) if the
    title isn't listed in the bulletin, or if no date can be parsed for it
    -- the date is never guessed or inferred from another entry.
    """
    full_text = _normalize("\n".join(page.text for page in bulletin_pages))
    normalized_title = _normalize(title)

    start = full_text.lower().find(normalized_title.lower())
    if start == -1:
        return None, False

    window_start = start + len(normalized_title)
    window_end = window_start + _WINDOW_CHARS
    for marker in _WINDOW_STOP_MARKERS:
        marker_pos = full_text.find(marker, window_start)
        if marker_pos != -1:
            window_end = min(window_end, marker_pos)
    window = full_text[window_start:window_end]

    default_date = None
    exceptions: list[tuple[str, list[str]]] = []
    for match in _CLAUSE_RE.finditer(window):
        raw_date, raw_states = match.groups()
        parsed = parse_date(raw_date)
        if parsed is None:
            continue
        if raw_states is None:
            if default_date is None:
                default_date = parsed
        else:
            exceptions.append((parsed, _parse_state_list(raw_states)))

    for exception_date, states in exceptions:
        if geography in states:
            return exception_date, True

    return default_date, False
