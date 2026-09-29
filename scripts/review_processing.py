#!/usr/bin/env python3
"""deterministic relevance assessment + synthetic
claim-volume impact estimation for substantive/uncertain changes in the review queue"""
import csv
import json
import re
import sys
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

REVIEW_QUEUE_PATH = ROOT / "data" / "review" / "review_queue.json"
PROFILE_PATH = ROOT / "config" / "synthetic_hospital_profile.yaml"
CLAIM_VOLUME_PATH = ROOT / "data" / "synthetic_claim_volume.csv"
OUTPUT_PATH = ROOT / "data" / "review" / "final_review_queue.json"
WORKBOOK_PATH = ROOT / "data" / "review" / "final_review_queue.xlsx"

WORKBOOK_COLUMNS = [
    ("change_id", 16),
    ("document_id", 22),
    ("classification", 14),
    ("relevance", 16),
    ("relevance_reason", 42),
    ("potential_annual_claim_lines", 14),
    ("impact_note", 42),
    ("confidence", 10),
    ("review_status", 12),
    ("section", 16),
    ("change_type", 11),
    ("before_text", 40),
    ("after_text", 40),
    ("summary", 32),
    ("reason", 36),
    ("why_it_may_matter", 32),
    ("billing_setting", 16),
    ("service_area", 14),
    ("age_min", 8),
    ("age_max", 8),
    ("codes", 12),
    ("states", 10),
    ("plan_scope", 14),
    ("additional_data_needed", 22),
    ("revision_history_match", 12),
    ("revision_history_evidence", 32),
    ("effective_date", 12),
    ("source_url", 28),
    ("prior_policy_number", 16),
    ("current_policy_number", 16),
]

_HEADER_FILL = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True, size=12)
_WRAP_TOP = Alignment(wrap_text=True, vertical="top")
_RELEVANCE_FILL = {
    "relevant": PatternFill("solid", fgColor="FFC7CE"),
    "needs_investigation": PatternFill("solid", fgColor="FFEB9C"),
    "irrelevant": PatternFill("solid", fgColor="E2EFDA"),
}

_INCLUDED_CLASSIFICATIONS = {"substantive", "uncertain"}
_UNSPECIFIC_VALUES = (None, "unknown", "other")

# Full names for the small, static set of US state abbreviations we need to
# recognize in policy prose (e.g. "except for Illinois...") against the
# hospital profile's two-letter geography.state. Not a business-logic
# alias map -- just the standard state name/abbreviation correspondence.
_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}

# "This policy applies ... except for Illinois, Maryland, ... and Wisconsin."
# -- an explicit exclusion-list pattern whose polarity we can resolve
# directly from the passage instead of treating any named state as
# ambiguous.
_EXCLUSION_LIST_RE = re.compile(r"except for\s+([^.]+?)\.", re.IGNORECASE)

# "Under 16 years of age" / "Under 18 years of age" -- an explicit age
# threshold whose before/after values define the population newly
# added/removed by a threshold change.
_AGE_THRESHOLD_RE = re.compile(r"[Uu]nder\s+(\d{1,3})\s+years?\s+of\s+age")


def _detect_state_exclusion_polarity(before_text: str | None, after_text: str | None, profile_state: str) -> bool | None:
    """Return True if the passage explicitly excludes profile_state, False
    if it explicitly names an exclusion list that does not include
    profile_state, or None if no explicit "except for ..." exclusion list
    is found in either text."""
    state_name = _US_STATE_NAMES.get(profile_state)
    if not state_name:
        return None
    for text in (after_text, before_text):
        if not text:
            continue
        match = _EXCLUSION_LIST_RE.search(text)
        if match:
            return state_name.lower() in match.group(1).lower()
    return None


def _detect_age_threshold_delta(before_text: str | None, after_text: str | None) -> tuple[int, int] | None:
    """Return the inclusive (lo, hi) age band newly added or removed by an
    explicit "Under N years of age" threshold change, or None if no such
    before/after pair is found. "Under 16" -> "Under 18" yields (16, 17):
    the population the changed threshold newly covers, not the full 0-17
    resulting range."""
    if not before_text or not after_text:
        return None
    before_match = _AGE_THRESHOLD_RE.search(before_text)
    after_match = _AGE_THRESHOLD_RE.search(after_text)
    if not before_match or not after_match:
        return None
    before_n, after_n = int(before_match.group(1)), int(after_match.group(1))
    if before_n == after_n:
        return None
    lo, hi = sorted((before_n, after_n))
    return lo, hi - 1


def load_profile(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_claim_volume(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        return [
            {
                "payer": row["payer"],
                "plan_scope": row["plan_scope"],
                "service_area": row["service_area"],
                "billing_setting": row["billing_setting"],
                "age_min": int(row["age_min"]),
                "age_max": int(row["age_max"]),
                "code": row["code"] or None,
                "service_description": row["service_description"],
                "annual_claim_lines": int(row["annual_claim_lines"]),
            }
            for row in reader
        ]


def assess_relevance(record: dict, profile: dict) -> tuple[str, str]:
    """Deterministic relevance decision from the change record's structured
    fields against the synthetic hospital profile. Never consults
    why_it_may_matter or any other LLM prose -- only service_area,
    billing_setting, age_min/age_max, plan_scope, and states, all of which
    were themselves extracted from the changed passage during detection."""
    service_area = record.get("service_area") or "unknown"
    billing_setting = record.get("billing_setting") or "unknown"
    age_min = record.get("age_min")
    age_max = record.get("age_max")
    plan_scope = record.get("plan_scope") or []
    states = record.get("states") or []

    profile_service_areas = set(profile["service_areas"])
    profile_billing_settings = set(profile["billing_settings"])
    profile_plans = set(profile["payer_scope"]["plans"])
    profile_state = profile["geography"]["state"]
    profile_age_min = profile["population"]["age_min"]
    profile_age_max = profile["population"]["age_max"]

    if service_area not in ("unknown", "other") and service_area not in profile_service_areas:
        return "irrelevant", f"service_area '{service_area}' is not one of the hospital's service areas."

    if billing_setting not in ("unknown", "other") and billing_setting not in profile_billing_settings:
        return "irrelevant", f"billing_setting '{billing_setting}' is not one of the hospital's billing settings."

    if age_min is not None or age_max is not None:
        rec_min = age_min if age_min is not None else 0
        rec_max = age_max if age_max is not None else 200
        if rec_max < profile_age_min or rec_min > profile_age_max:
            return "irrelevant", (
                f"stated age range {rec_min}-{rec_max} does not overlap the hospital's pediatric "
                f"population ({profile_age_min}-{profile_age_max})."
            )

    if plan_scope and not (set(plan_scope) & profile_plans):
        return "irrelevant", (
            f"plan_scope {plan_scope} does not overlap the hospital's payer plans {sorted(profile_plans)}."
        )

    if states:
        # Resolve directly from the passage's own before/after text when
        # its exclusion-list polarity is explicit (e.g. "except for
        # Illinois, ... and Wisconsin."); only fall back to flagging
        # ambiguity when no such explicit pattern is found.
        excludes_hospital_state = _detect_state_exclusion_polarity(
            record.get("before_text"), record.get("after_text"), profile_state
        )
        if excludes_hospital_state is True:
            return "relevant", (
                f"passage explicitly excludes the hospital's state ({profile_state}) from applicability "
                f"-- this directly changes coverage for the hospital."
            )
        if excludes_hospital_state is False:
            return "irrelevant", (
                f"passage explicitly lists an exclusion of {states} that does not include the "
                f"hospital's state ({profile_state}); this specific change doesn't affect the hospital."
            )
        return "needs_investigation", (
            f"passage names specific state(s) {states}; whether this includes or excludes the "
            f"hospital's state ({profile_state}) can't be confirmed from the structured fields alone."
        )

    if service_area in ("unknown", "other") or billing_setting in ("unknown", "other"):
        return "needs_investigation", (
            f"service_area='{service_area}' and/or billing_setting='{billing_setting}' aren't specific "
            "enough to confirm applicability to the hospital's profile."
        )

    return "relevant", (
        f"service_area '{service_area}' and billing_setting '{billing_setting}' both fall within the "
        "hospital's profile, with no conflicting age, plan, or geography signal."
    )


def match_claim_rows(record: dict, claim_rows: list[dict]) -> tuple[list[dict], str]:
    """Strict priority cascade -- no soft fallback between rules:
      1. explicit plan_scope -> that exact plan_scope only
      2. explicit code -> that exact code only
      3. detected age-threshold delta -> only the newly affected age band
      4. explicit age_min/age_max fields (no delta pattern found) -> that range
      5. otherwise -> service_area + billing_setting, an explicitly
         labeled upper-bound proxy
    Each of rules 1-4 is a hard requirement once its trigger condition is
    present: if it yields no rows, that IS the answer (None) -- we never
    relax it into the broader rule 5 estimate. service_area is always
    required; billing_setting narrows the base set whenever it's a known
    value, before any of rules 1-4 apply."""
    base = [r for r in claim_rows if r["service_area"] == record.get("service_area")]
    billing_setting = record.get("billing_setting")
    if billing_setting not in _UNSPECIFIC_VALUES:
        base = [r for r in base if r["billing_setting"] == billing_setting]

    plan_scope = record.get("plan_scope") or []
    if plan_scope:
        return [r for r in base if r["plan_scope"] in plan_scope], "plan_scope"

    codes = record.get("codes") or []
    if codes:
        return [r for r in base if r["code"] and r["code"] in codes], "code"

    age_band = _detect_age_threshold_delta(record.get("before_text"), record.get("after_text"))
    if age_band:
        lo, hi = age_band
        return [r for r in base if not (r["age_max"] < lo or r["age_min"] > hi)], "age_threshold_delta"

    age_min, age_max = record.get("age_min"), record.get("age_max")
    if age_min is not None or age_max is not None:
        lo = age_min if age_min is not None else 0
        hi = age_max if age_max is not None else 200
        return [r for r in base if not (r["age_max"] < lo or r["age_min"] > hi)], "age_range"

    return base, "service_area_billing_setting_proxy"


def estimate_claim_impact(record: dict, claim_rows: list[dict]) -> tuple[int | None, str]:
    matches, basis = match_claim_rows(record, claim_rows)

    if not matches:
        if basis == "plan_scope":
            return None, (
                f"No synthetic claim-volume rows exist for plan_scope={record.get('plan_scope')} within "
                f"service_area='{record.get('service_area')}'; potential exposure can't be estimated -- "
                "broader (e.g. Commercial) rows are not used as a substitute for an explicit plan_scope."
            )
        if basis == "code":
            return None, (
                f"No synthetic claim-volume row exists for code(s) {record.get('codes')}; code-specific "
                "volume is unavailable, and broader service-area volume is not used as a substitute."
            )
        if basis in ("age_threshold_delta", "age_range"):
            return None, (
                "No synthetic claim-volume rows overlap the stated age band for this change; potential "
                "exposure can't be estimated for that specific band."
            )
        return None, (
            f"No synthetic claim-volume rows matched service_area='{record.get('service_area')}'; "
            "potential exposure can't be estimated from the available claim data."
        )

    total = sum(r["annual_claim_lines"] for r in matches)

    if basis == "plan_scope":
        note = (
            f"Synthetic potential-exposure estimate: {total} annual claim lines from {len(matches)} "
            f"claim-volume row(s) matched on the change's exact plan_scope {record.get('plan_scope')}."
        )
    elif basis == "code":
        note = (
            f"Synthetic potential-exposure estimate: {total} annual claim lines from {len(matches)} "
            f"claim-volume row(s) matched on the change's exact code(s) {record.get('codes')}."
        )
    elif basis == "age_threshold_delta":
        lo, hi = _detect_age_threshold_delta(record.get("before_text"), record.get("after_text"))
        note = (
            f"Synthetic potential-exposure estimate: {total} annual claim lines from {len(matches)} "
            f"claim-volume row(s) covering ages {lo}-{hi} -- the population newly added/removed by this "
            "age-threshold change, not the full resulting age range."
        )
    elif basis == "age_range":
        note = (
            f"Synthetic potential-exposure estimate: {total} annual claim lines from {len(matches)} "
            f"claim-volume row(s) matched on the stated age range."
        )
    else:
        note = (
            f"Upper-bound proxy estimate: {total} annual claim lines from {len(matches)} claim-volume "
            f"row(s) matched only on service_area/billing_setting. No code, plan_scope, or age-threshold "
            "delta was available to narrow further, so this is a broad upper bound, not a precise estimate."
        )
    return total, note


def process_review_queue(records: list[dict], profile: dict, claim_rows: list[dict]) -> list[dict]:
    processed = []
    for record in records:
        record = dict(record)

        if record["classification"] in _INCLUDED_CLASSIFICATIONS:
            relevance, relevance_reason = assess_relevance(record, profile)
            if relevance in ("relevant", "needs_investigation"):
                potential_annual_claim_lines, impact_note = estimate_claim_impact(record, claim_rows)
            else:
                potential_annual_claim_lines = None
                impact_note = "Not estimated: change assessed as irrelevant to the hospital profile."
        else:
            relevance = relevance_reason = potential_annual_claim_lines = impact_note = None

        record["relevance"] = relevance
        record["relevance_reason"] = relevance_reason
        record["potential_annual_claim_lines"] = potential_annual_claim_lines
        record["impact_note"] = impact_note
        processed.append(record)
    return processed


def build_workbook(records: list[dict], output_path: Path) -> None:
    """Human-readable rendering of the final review queue, generated
    alongside the JSON on every run -- not a separate manual step."""
    wb = Workbook()
    ws = wb.active
    ws.title = "final_review_queue"

    ws.cell(row=1, column=1, value=f"final_review_queue.json -- {len(records)} total candidates").font = (
        _SECTION_FONT
    )

    header_row = 2
    for col_index, (name, width) in enumerate(WORKBOOK_COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col_index, value=name)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        ws.column_dimensions[get_column_letter(col_index)].width = width

    relevance_col = next(i for i, (f, _w) in enumerate(WORKBOOK_COLUMNS, start=1) if f == "relevance")

    row = header_row + 1
    for record in records:
        for col_index, (field, _width) in enumerate(WORKBOOK_COLUMNS, start=1):
            value = record.get(field)
            if isinstance(value, list):
                value = ", ".join(value)
            ws.cell(row=row, column=col_index, value=value).alignment = _WRAP_TOP
        fill = _RELEVANCE_FILL.get(record.get("relevance"))
        if fill:
            ws.cell(row=row, column=relevance_col).fill = fill
        row += 1

    last_col_letter = get_column_letter(len(WORKBOOK_COLUMNS))
    ws.auto_filter.ref = f"A{header_row}:{last_col_letter}{row - 1}"
    ws.freeze_panes = f"A{header_row + 1}"

    wb.save(output_path)


def main() -> int:
    records = json.loads(REVIEW_QUEUE_PATH.read_text())
    profile = load_profile(PROFILE_PATH)
    claim_rows = load_claim_volume(CLAIM_VOLUME_PATH)

    processed = process_review_queue(records, profile, claim_rows)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(processed, indent=2))
    build_workbook(processed, WORKBOOK_PATH)

    counts: dict[str, int] = {}
    for r in processed:
        if r["relevance"] is not None:
            counts[r["relevance"]] = counts.get(r["relevance"], 0) + 1

    print(f"OK      {len(processed)} record(s) -> {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"OK      wrote {WORKBOOK_PATH.relative_to(ROOT)}")
    print(f"        relevance counts: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
