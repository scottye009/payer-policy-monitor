from datetime import datetime

_DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y", "%m.%d.%Y"]


def parse_date(raw: str) -> str | None:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None
