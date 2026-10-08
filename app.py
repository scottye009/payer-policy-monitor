"""Lightweight human-review layer over data/review/final_review_queue.json."""
import base64
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "data" / "review" / "final_review_queue.json"
RAW_DIR = ROOT / "data" / "raw"
SIMULATED_PRIOR_RAW_DIR = ROOT / "data" / "simulated_prior_raw"
COMPLETED_JSON_PATH = ROOT / "data" / "review" / "completed_review.json"
COMPLETED_CSV_PATH = ROOT / "data" / "review" / "completed_review_summary.csv"

sys.path.insert(0, str(ROOT / "src"))
from payer_policy.source_check import MONITORED_POLICY_IDS, check_for_updates, get_status  # noqa: E402

POLICY_LABELS = {
    "mri_ct_site_of_service": "MRI/CT Site of Service",
    "sleep_studies": "Sleep Studies",
}

# The same three prior/current pairs scripts/detect_changes.py uses -- not
# duplicated pipeline logic, just the display layer's way of finding each
# document's corresponding simulated-prior PDF for the "Prior version" tab.
PRIOR_PDF_BY_DOCUMENT_ID = {
    "mri_ct_site_of_service": "mri_ct_prior",
    "spinraza": "spinraza_prior",
    "sleep_studies": "sleep_studies_prior",
}

DECISION_OPTIONS = ["(none)", "Relevant", "Irrelevant", "Needs investigation"]
DECISION_TO_VALUE = {
    "Relevant": "relevant",
    "Irrelevant": "irrelevant",
    "Needs investigation": "needs_investigation",
}
VALUE_TO_DECISION = {v: k for k, v in DECISION_TO_VALUE.items()}
REQUIRED_RELEVANCE = {"relevant", "needs_investigation"}


@st.cache_data
def load_queue() -> list[dict]:
    return json.loads(QUEUE_PATH.read_text())


def get_review(change_id: str) -> dict:
    return st.session_state.reviews.setdefault(change_id, {"reviewer_decision": None, "reviewer_note": ""})


def is_reviewed(change_id: str) -> bool:
    return bool(st.session_state.reviews.get(change_id, {}).get("reviewer_decision"))


def next_pending_change_id(items: list[dict], after_change_id: str | None) -> str | None:
    """First not-yet-reviewed item in `items`, starting just after
    `after_change_id` and wrapping around; falls back to the first pending
    item overall if `after_change_id` isn't in `items`."""
    ids = [item["change_id"] for item in items]
    start = ids.index(after_change_id) + 1 if after_change_id in ids else 0
    rotated = items[start:] + items[:start]
    return next((item["change_id"] for item in rotated if not is_reviewed(item["change_id"])), None)


def render_pdf(pdf_path: Path, page: int | None, key: str) -> None:
    if not pdf_path.exists():
        st.info(f"PDF not found at `{pdf_path.relative_to(ROOT)}`.")
        return
    data = pdf_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    fragment = f"#page={page}" if page else ""
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{b64}{fragment}" '
        f'width="100%" height="600" style="border:1px solid #ddd;" title="{key}"></iframe>',
        unsafe_allow_html=True,
    )


def build_completed_records(records: list[dict]) -> list[dict]:
    completed = []
    for record in records:
        review = st.session_state.reviews.get(record["change_id"], {})
        decision = review.get("reviewer_decision")
        completed.append(
            {
                **record,
                "reviewer_decision": decision,
                "reviewer_note": review.get("reviewer_note") or "",
                "reviewed_at": review.get("reviewed_at") if decision else None,
            }
        )
    return completed


def build_summary_csv(completed_records: list[dict]) -> str:
    fields = [
        "change_id", "document_id", "classification", "relevance", "reviewer_decision",
        "reviewer_note", "effective_date", "potential_annual_claim_lines", "summary",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for record in completed_records:
        writer.writerow(record)
    return buf.getvalue()


def short_hash(value: str | None) -> str:
    return value[:12] if value else "—"


def render_policy_update_check() -> None:
    """Network is only touched on an explicit button click; every render,
    including the one right after a click, reads persisted state via get_status()."""
    st.subheader("Policy update check")
    if st.button("Check for updates"):
        with st.spinner("Checking UHC sources..."):
            for policy_id in MONITORED_POLICY_IDS:
                check_for_updates(policy_id)

    rows = []
    for policy_id in MONITORED_POLICY_IDS:
        label = POLICY_LABELS.get(policy_id, policy_id)
        status = get_status(policy_id)
        if status is None:
            rows.append({"policy": label, "status": "Not checked yet"})
            continue

        status_label = status.status.capitalize()
        if status.status == "initialized" and status.current_is_simulated:
            status_label += " (simulated demo baseline)"
        rows.append(
            {
                "policy": label,
                "status": status_label,
                "checked_at": status.checked_at,
                "previous_hash": short_hash(status.previous_hash),
                "current_hash": short_hash(status.current_hash),
                "current_version": (
                    "—" if status.current_is_simulated is None
                    else "simulated prior" if status.current_is_simulated
                    else "live UHC PDF"
                ),
                "error": status.error or "",
            }
        )
        if status.status == "failed":
            st.error(f"{label}: Failed — {status.error}. Last good version kept.")

    st.dataframe(rows, use_container_width=True, hide_index=True)


st.set_page_config(page_title="Payer Policy Change Review", layout="wide")

if "reviews" not in st.session_state:
    st.session_state.reviews = {}

queue = load_queue()
required_ids = {r["change_id"] for r in queue if r.get("relevance") in REQUIRED_RELEVANCE}
completed_required = sum(1 for cid in required_ids if is_reviewed(cid))

st.title("Payer Policy Change Review")
st.caption(
    "Thin review layer over data/review/final_review_queue.json. Does not call an LLM or "
    "modify upstream detection/relevance/impact data."
)

# --- Policy update check -------------------------------------------------------
render_policy_update_check()
st.divider()

# --- Sidebar: filters + progress -------------------------------------------------
with st.sidebar:
    st.header("Filters")
    show_all = st.checkbox(
        "Show all items (including non_substantive, irrelevant)",
        value=False,
        help="Default view emphasizes the substantive/uncertain, non-irrelevant items that actually need review.",
    )
    relevance_options = sorted({str(r.get("relevance")) for r in queue})
    classification_options = sorted({r["classification"] for r in queue})
    status_options = ["(any)", "reviewed", "not reviewed"]

    relevance_filter = st.multiselect("Relevance", relevance_options, default=[])
    classification_filter = st.multiselect("Classification", classification_options, default=[])
    status_filter = st.selectbox("Review status", status_options, index=0)

    st.divider()
    st.header("Required reviews")
    st.write(f"**{completed_required} / {len(required_ids)}** relevant / needs_investigation items reviewed")
    st.progress(completed_required / len(required_ids) if required_ids else 1.0)

# --- Apply filters -----------------------------------------------------------
# By default, irrelevant and non_substantive items are hidden -- but an
# explicit filter selection (e.g. picking "irrelevant" in the Relevance
# multiselect) always overrides that default and brings them back.
visible = list(queue)
if not show_all:
    if not relevance_filter:
        visible = [r for r in visible if r.get("relevance") != "irrelevant"]
    if not classification_filter:
        visible = [r for r in visible if r["classification"] != "non_substantive"]

if relevance_filter:
    visible = [r for r in visible if str(r.get("relevance")) in relevance_filter]
if classification_filter:
    visible = [r for r in visible if r["classification"] in classification_filter]
if status_filter == "reviewed":
    visible = [r for r in visible if is_reviewed(r["change_id"])]
elif status_filter == "not reviewed":
    visible = [r for r in visible if not is_reviewed(r["change_id"])]


def _sort_key(r: dict) -> tuple:
    claim_lines = r.get("potential_annual_claim_lines")
    effective_date = r.get("effective_date")
    return (
        claim_lines is None,
        -(claim_lines or 0),
        effective_date is None,
        effective_date or "",
    )


visible = sorted(visible, key=_sort_key)

table_rows = [
    {
        "change_id": r["change_id"],
        "document": r["document_id"],
        "classification": r["classification"],
        "relevance": r.get("relevance"),
        "effective_date": r.get("effective_date"),
        "potential_annual_claim_lines": r.get("potential_annual_claim_lines"),
        "reviewer_status": "reviewed" if is_reviewed(r["change_id"]) else "pending",
    }
    for r in visible
]

with st.expander(f"Queue ({len(visible)} of {len(queue)} shown)", expanded=True):
    selection_event = st.dataframe(
        table_rows,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="queue_table",
    )
    selected_rows = selection_event.selection.rows if selection_event and selection_event.selection else []
    if selected_rows:
        st.session_state.selected_change_id = visible[selected_rows[0]]["change_id"]

visible_ids = [r["change_id"] for r in visible]
if st.session_state.get("selected_change_id") not in visible_ids:
    st.session_state.selected_change_id = next_pending_change_id(visible, None) or (
        visible_ids[0] if visible_ids else None
    )

selected_record = next(
    (r for r in visible if r["change_id"] == st.session_state.selected_change_id), None
)

st.divider()

# --- Detail panel -------------------------------------------------------------
if selected_record is None:
    st.info("No items match the current filters.")
else:
    r = selected_record
    change_id = r["change_id"]

    st.subheader(f"{r['document_id']}  ·  {change_id}")
    badge_cols = st.columns(4)
    badge_cols[0].metric("Classification", r["classification"])
    badge_cols[1].metric("Automated relevance", str(r.get("relevance")))
    badge_cols[2].metric("Confidence", r.get("confidence"))
    badge_cols[3].metric("Potential annual claim lines", r.get("potential_annual_claim_lines"))

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Summary**")
        st.write(r.get("summary") or "—")
        st.markdown("**Why it may matter**")
        st.write(r.get("why_it_may_matter") or "—")
        if r.get("impact_note"):
            st.markdown("**Impact note**")
            st.write(r["impact_note"])
    with right:
        st.markdown("**Billing setting**")
        st.write(r.get("billing_setting") or "unknown")
        st.markdown("**Service area**")
        st.write(r.get("service_area") or "unknown")
        st.markdown("**Effective date**")
        st.write(r.get("effective_date") or "—")
        st.markdown("**Section / page**")
        st.write(
            f"{r.get('section') or '—'} "
            f"(prior page {r.get('prior_page') or '—'}, current page {r.get('current_page') or '—'})"
        )
        if r.get("source_url"):
            st.markdown(f"[Source policy PDF]({r['source_url']})")

    st.markdown("#### Supporting evidence")
    ev_left, ev_right = st.columns(2)
    with ev_left:
        st.markdown("**BEFORE**")
        st.text_area("before_text", r.get("before_text") or "(none -- this passage was added)", height=180,
                      key=f"before_{change_id}", disabled=True, label_visibility="collapsed")
    with ev_right:
        st.markdown("**AFTER**")
        st.text_area("after_text", r.get("after_text") or "(none -- this passage was removed)", height=180,
                      key=f"after_{change_id}", disabled=True, label_visibility="collapsed")

    if r.get("revision_history_evidence"):
        st.markdown(f"**Policy History match** (`revision_history_match={r['revision_history_match']}`)")
        st.text_area("revision_history_evidence", r["revision_history_evidence"], height=100,
                      key=f"history_{change_id}", disabled=True, label_visibility="collapsed")
    else:
        st.caption(f"Policy History match: {r.get('revision_history_match')} (no matching passage found)")

    st.markdown("#### Source PDF")
    current_pdf = RAW_DIR / f"{r['document_id']}.pdf"
    prior_id = PRIOR_PDF_BY_DOCUMENT_ID.get(r["document_id"])
    prior_pdf = (SIMULATED_PRIOR_RAW_DIR / f"{prior_id}.pdf") if prior_id else None

    if prior_pdf is not None:
        tab_current, tab_prior = st.tabs(["Current policy", "Prior version"])
        with tab_current:
            render_pdf(current_pdf, r.get("current_page"), key=f"current_{change_id}")
        with tab_prior:
            st.warning("SIMULATED PRIOR — not an original payer artifact")
            render_pdf(prior_pdf, r.get("prior_page"), key=f"prior_{change_id}")
    else:
        st.caption("No simulated-prior PDF is associated with this document.")
        render_pdf(current_pdf, r.get("current_page"), key=f"current_{change_id}")

    st.markdown("#### Reviewer decision")
    review = get_review(change_id)
    required = r.get("relevance") in REQUIRED_RELEVANCE
    if required:
        st.caption("This item's automated relevance requires a reviewer decision before submission.")

    current_label = VALUE_TO_DECISION.get(review["reviewer_decision"], "(none)")
    chosen = st.radio(
        "Reviewer decision", DECISION_OPTIONS, index=DECISION_OPTIONS.index(current_label),
        key=f"decision_{change_id}", horizontal=True,
    )
    note = st.text_area(
        "Reviewer note (optional)", value=review.get("reviewer_note", ""), key=f"note_{change_id}",
    )

    new_decision = DECISION_TO_VALUE.get(chosen)
    if new_decision != review["reviewer_decision"] or note != review.get("reviewer_note"):
        decision_just_set = new_decision and not review["reviewer_decision"]
        review["reviewer_decision"] = new_decision
        review["reviewer_note"] = note
        review["reviewed_at"] = datetime.now(timezone.utc).isoformat() if new_decision else None
        if decision_just_set:
            next_id = next_pending_change_id(visible, change_id)
            if next_id:
                st.session_state.selected_change_id = next_id
        st.rerun()

st.divider()

# --- Submission ----------------------------------------------------------------
st.subheader("Submit completed review")
st.write(f"Required reviews completed: **{completed_required} / {len(required_ids)}**")

can_submit = completed_required == len(required_ids) and len(required_ids) > 0
if not can_submit:
    missing = len(required_ids) - completed_required
    st.warning(
        f"{missing} required item(s) (automated relevance = relevant or needs_investigation) still "
        "need a reviewer decision before the final review can be submitted."
    )

if st.button("Submit completed review", disabled=not can_submit, type="primary"):
    completed_records = build_completed_records(queue)
    COMPLETED_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPLETED_JSON_PATH.write_text(json.dumps(completed_records, indent=2))
    COMPLETED_CSV_PATH.write_text(build_summary_csv(completed_records))
    st.session_state.submitted = True
    st.success(f"Wrote {COMPLETED_JSON_PATH.relative_to(ROOT)} and {COMPLETED_CSV_PATH.relative_to(ROOT)}.")

if st.session_state.get("submitted"):
    completed_records = build_completed_records(queue)
    st.download_button(
        "Download completed_review.json",
        data=json.dumps(completed_records, indent=2),
        file_name="completed_review.json",
        mime="application/json",
    )
    st.download_button(
        "Download completed_review_summary.csv",
        data=build_summary_csv(completed_records),
        file_name="completed_review_summary.csv",
        mime="text/csv",
    )
