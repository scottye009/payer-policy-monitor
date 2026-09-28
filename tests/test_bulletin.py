from payer_policy.bulletin import find_bulletin_effective_date
from payer_policy.models import PageText

BULLETIN_PAGE = PageText(
    page_number=1,
    text=(
        "New\n"
        "Policy Title\n"
        "Effective Date\n"
        "Policy Summary\n"
        "Routine Test\n"
        "Management - Allergen\n"
        "Testing Policy,\n"
        "Professional and\n"
        "Facility - Reminder\n"
        "9/1/2026\n"
        "10/1/2026 for\n"
        "AR, CO, KY,\n"
        "NC, NE, OH,\n"
        "and RI.\n"
        "Effective for dates of service on or after September 1, 2026, UnitedHealthcare will implement the "
        "new Allergen Testing Policy, Professional and Facility.\n"
    ),
)

TITLE = "Allergen Testing Policy, Professional and Facility"


def test_bulletin_default_effective_date_outside_exception_states():
    date, used_exception = find_bulletin_effective_date([BULLETIN_PAGE], TITLE, "WA")
    assert date == "2026-09-01"
    assert used_exception is False


def test_bulletin_state_exception_resolution():
    date, used_exception = find_bulletin_effective_date([BULLETIN_PAGE], TITLE, "CO")
    assert date == "2026-10-01"
    assert used_exception is True


def test_bulletin_returns_none_when_title_not_listed():
    date, used_exception = find_bulletin_effective_date([BULLETIN_PAGE], "Some Unrelated Policy", "WA")
    assert date is None
    assert used_exception is False
