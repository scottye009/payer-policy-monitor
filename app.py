"""Payer policy review app: live policy monitoring (source checks, analysis,
persisted findings and reviews) plus the static take-home review queue over
data/review/final_review_queue.json."""
import base64
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "data" / "review" / "final_review_queue.json"
RAW_DIR = ROOT / "data" / "raw"
SIMULATED_PRIOR_RAW_DIR = ROOT / "data" / "simulated_prior_raw"
COMPLETED_JSON_PATH = ROOT / "data" / "review" / "completed_review.json"
COMPLETED_CSV_PATH = ROOT / "data" / "review" / "completed_review_summary.csv"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from payer_policy.review_store import (  # noqa: E402
    REVIEW_STATUSES,
    clear_findings,
    completed_reviews,
    list_findings,
    save_findings,
    save_review,
)
from payer_policy.source_check import (  # noqa: E402
    MONITORED_POLICY_IDS,
    check_for_updates,
    clear_source_state,
    get_status,
    get_version_by_hash,
)
from update_pipeline import run_update_pipeline  # noqa: E402

POLICY_LABELS = {
    "mri_ct_site_of_service": "MRI/CT Site of Service",
    "sleep_studies": "Sleep Studies",
    "surgery_elbow": "Surgery of the Elbow",
    "home_health_care": "Home Health Care",
    "spinraza": "Spinraza (failure test)",
}
# How each version origin is named in the UI.
ORIGIN_LABELS = {
    "simulated_prior": "simulated prior",
    "local_prior": "local prior PDF",
    "live_fetch": "live UHC PDF",
}
INITIALIZED_SUFFIX = {
    "simulated_prior": " (simulated demo baseline)",
    "local_prior": " (real prior UHC PDF, provided locally)",
}
CONFIG_PATH = ROOT / "config" / "sources.yaml"

# Streamlit gives each keyed widget's container a "st-key-<key>" class; this
# colours only the "Analyze all updates" button green (other primary buttons
# keep the theme colour).
ANALYZE_ALL_CSS = """
<style>
.st-key-analyze_all button { background-color: #1e8e3e; border-color: #1e8e3e; color: #ffffff; }
.st-key-analyze_all button:hover { background-color: #17733a; border-color: #17733a; color: #ffffff; }
.st-key-analyze_all button:active { background-color: #135f30; border-color: #135f30; color: #ffffff; }
</style>
"""

REVIEW_ICONS = {"pending": "⏳", "reviewed": "✅", "dismissed": "🚫"}

EXPORT_CSV_FIELDS = [
    "finding_id", "policy_id", "previous_hash", "current_hash", "review_status", "review_note",
    "reviewed_at", "change_type", "section", "prior_page", "current_page", "before_text", "after_text",
    "effective_date", "source_url", "classification", "relevance", "summary",
]

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
    # Explicit zoom: a viewer that loads inside a not-yet-visible tab measures
    # ~0 width and would otherwise "fit" the page to a tiny size.
    fragment = f"#page={page}&zoom=100" if page else "#zoom=100"
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
    statuses = {}
    for policy_id in MONITORED_POLICY_IDS:
        label = POLICY_LABELS.get(policy_id, policy_id)
        status = statuses[policy_id] = get_status(policy_id)
        if status is None:
            rows.append({"policy": label, "status": "Not checked yet"})
            continue

        status_label = status.status.capitalize()
        if status.status == "initialized":
            status_label += INITIALIZED_SUFFIX.get(status.current_origin, "")
        rows.append(
            {
                "policy": label,
                "status": status_label,
                "checked_at": status.checked_at,
                "previous_hash": short_hash(status.previous_hash),
                "current_hash": short_hash(status.current_hash),
                "current_version": ORIGIN_LABELS.get(status.current_origin, "—"),
                "error": status.error or "",
            }
        )
        if status.status == "failed":
            st.error(f"{label}: Failed — {status.error}. Last good version kept.")

    st.dataframe(rows, use_container_width=True, hide_index=True)
    render_analyze_changes(statuses)


def saved_count(policy_id: str, status) -> int:
    """Findings already saved for this check's exact version pair."""
    return sum(
        1 for f in list_findings(policy_id)
        if (f.previous_hash, f.current_hash) == (status.previous_hash, status.current_hash)
    )


def analyze_policy(policy_id: str, status) -> None:
    """Run the existing pipeline for one UPDATED version pair and save its
    findings. The outcome is queued and shown after the follow-up rerun."""
    label = POLICY_LABELS.get(policy_id, policy_id)
    messages = st.session_state.setdefault("analysis_messages", [])
    try:
        result = run_update_pipeline(policy_id, status)
    except Exception as exc:
        messages.append(("error", f"{label}: analysis failed — {exc}"))
        return
    added = save_findings(policy_id, result.previous_hash, result.current_hash, result.findings)
    messages.append(("success", f"{label}: {len(result.findings)} finding(s) for this comparison, {added} new."))


def render_analyze_changes(statuses: dict) -> None:
    """Only an UPDATED check offers analysis, and only an explicit click runs
    the pipeline (MPNet + Qwen). Findings go straight to SQLite; re-analyzing
    the same version pair adds nothing and never resets a review."""
    # Outcomes of the analysis that triggered this rerun.
    for kind, text in st.session_state.pop("analysis_messages", []):
        (st.error if kind == "error" else st.success)(text)

    updated = {pid: s for pid, s in statuses.items() if s is not None and s.status == "updated"}
    if not updated:
        return

    not_analyzed = [pid for pid, s in updated.items() if saved_count(pid, s) == 0]
    if not_analyzed:
        names = ", ".join(POLICY_LABELS.get(pid, pid) for pid in not_analyzed)
        st.markdown(ANALYZE_ALL_CSS, unsafe_allow_html=True)
        if st.button(f"Analyze all updates ({len(not_analyzed)})", key="analyze_all", type="primary",
                     help=f"Runs analysis for: {names}. Policies already analyzed are skipped."):
            with st.status(f"Analyzing {len(not_analyzed)} updated policies...", expanded=True) as progress:
                for i, policy_id in enumerate(not_analyzed, start=1):
                    st.write(f"{i}/{len(not_analyzed)} · {POLICY_LABELS.get(policy_id, policy_id)}")
                    analyze_policy(policy_id, updated[policy_id])
                progress.update(label="Analysis finished", state="complete")
            # Rerun so button counts, captions and the findings list reflect the new findings.
            st.rerun()
    else:
        st.caption("All updated policies have been analyzed.")

    for policy_id, status in updated.items():
        label = POLICY_LABELS.get(policy_id, policy_id)
        if st.button(f"Analyze changes — {label}", key=f"analyze_{policy_id}"):
            with st.spinner(f"Analyzing {label} (MPNet alignment + Qwen adjudication)..."):
                analyze_policy(policy_id, status)
            st.rerun()
        elif saved := saved_count(policy_id, status):
            st.caption(
                f"{label}: {saved} finding(s) already saved for this comparison. Re-analyzing "
                "won't duplicate them or reset reviews."
            )


@st.cache_data
def policy_titles() -> dict[str, str]:
    documents = yaml.safe_load(CONFIG_PATH.read_text())["documents"]
    return {d["id"]: d["title"] for d in documents}


def is_actionable(f) -> bool:
    return f.record["classification"] != "non_substantive"


def render_live_monitoring() -> None:
    # Filled in last, so counts include findings saved or reviewed this run.
    summary = st.empty()

    render_policy_update_check()
    st.divider()
    render_monitored_findings()
    st.divider()
    render_export_completed_reviews()
    st.divider()
    render_demo_controls()

    actionable = [f for f in list_findings() if is_actionable(f)]
    pending = sum(1 for f in actionable if f.review_status == "pending")
    with summary.container():
        st.markdown(f"### Required review: {pending} pending / {len(actionable)} actionable findings")
        st.caption("Actionable = substantive or uncertain. non_substantive findings stay available below.")


def render_monitored_findings() -> None:
    """Findings from analyzed version pairs, read from SQLite on every render
    so review state survives reruns, restarts, and later source checks."""
    st.subheader("Findings")
    findings = list_findings()
    if not findings:
        st.caption("No saved findings yet. Click Analyze changes on an Updated policy.")
        return

    show_all = st.checkbox("Show all findings (including non_substantive)", value=False, key="mf_show_all")
    visible = [f for f in findings if show_all or is_actionable(f)]
    visible.sort(key=lambda f: (f.review_status != "pending", not is_actionable(f)))
    st.caption(
        f"{len(visible)} of {len(findings)} findings shown · pending first · "
        + " · ".join(f"{icon} {status}" for status, icon in REVIEW_ICONS.items())
    )
    if not visible:
        return

    for f in visible:
        with st.expander(finding_label(f)):
            render_monitored_finding(f)


def finding_label(f) -> str:
    r = f.record
    summary = r.get("summary") or "—"
    if len(summary) > 90:
        summary = summary[:87] + "..."
    return (
        f"{REVIEW_ICONS[f.review_status]} {POLICY_LABELS.get(f.policy_id, f.policy_id)} · "
        f"{r.get('section') or 'no section'} · {r['classification']} · {summary}"
    )


def _version_line(name: str, version) -> str:
    if version is None:
        return f"- **{name}:** not found"
    kind = {
        "simulated_prior": "SIMULATED prior (registered, not downloaded)",
        "local_prior": "real prior UHC PDF, provided locally (registered, not downloaded by the monitor)",
    }.get(version.origin, "live UHC PDF")
    timing = "retrieved" if version.origin == "live_fetch" else "registered"
    return (
        f"- **{name}:** {kind} · sha256 `{short_hash(version.content_sha256)}` · "
        f"`{version.snapshot_path}` · {timing} {version.retrieved_at}"
    )


def _listed(values) -> str:
    return ", ".join(values) if values else "not stated"


def render_monitored_finding(f) -> None:
    r = f.record
    fid = f.finding_id
    previous = get_version_by_hash(f.policy_id, f.previous_hash)
    current = get_version_by_hash(f.policy_id, f.current_hash)
    previous_is_simulated = bool(previous and previous.is_simulated)

    st.caption(
        f"Finding `{short_hash(fid)}` · comparison `{short_hash(f.previous_hash)}` → "
        f"`{short_hash(f.current_hash)}` · review status: {f.review_status}"
    )

    # --- 1. Source evidence: verbatim from config + captured PDFs ------------
    st.markdown("#### 1. Source evidence")
    st.caption("From config and the captured PDFs only — no model output in this section.")
    st.markdown(f"**Policy:** {policy_titles().get(f.policy_id, f.policy_id)}")
    source_url = current.source_url if current else r.get("source_url")
    if source_url:
        st.markdown(f"**Canonical source URL:** {source_url}")
    st.markdown(_version_line("Previous", previous) + "\n" + _version_line("Current", current))
    if previous_is_simulated:
        st.warning("Previous version is a SIMULATED PRIOR — not an original payer artifact.")
    st.markdown(
        f"**Change:** {r['change_type']} · **Section:** {r.get('section') or '—'} · "
        f"**Pages:** prior {r.get('prior_page') or '—'}, current {r.get('current_page') or '—'}"
    )
    st.markdown(
        f"**Effective date:** {r.get('effective_date') or 'not stated'} "
        "(extracted from the current PDF's own text)"
    )
    ev_left, ev_right = st.columns(2)
    with ev_left:
        st.markdown("**BEFORE**")
        st.text_area("mf_before", r.get("before_text") or "(none -- this passage was added)", height=180,
                     key=f"mf_before_{fid}", disabled=True, label_visibility="collapsed")
    with ev_right:
        st.markdown("**AFTER**")
        st.text_area("mf_after", r.get("after_text") or "(none -- this passage was removed)", height=180,
                     key=f"mf_after_{fid}", disabled=True, label_visibility="collapsed")
    # Collapsed expanders still render their contents, so embedded PDFs are
    # opt-in per finding to keep every rerun light.
    if st.toggle("Show snapshot PDFs", key=f"mf_pdfs_{fid}"):
        tab_current, tab_previous = st.tabs(["Current snapshot", "Previous snapshot"])
        with tab_current:
            if current:
                render_pdf(ROOT / current.snapshot_path, r.get("current_page"), key=f"mf_current_{fid}")
        with tab_previous:
            if previous_is_simulated:
                st.warning("SIMULATED PRIOR — not an original payer artifact")
            if previous:
                render_pdf(ROOT / previous.snapshot_path, r.get("prior_page"), key=f"mf_previous_{fid}")

    # --- 2. Automated interpretation: model output + deterministic rules -----
    st.markdown("#### 2. Automated interpretation")
    st.caption(
        "Generated by the pipeline: Qwen adjudication and model-extracted fields, then deterministic "
        "relevance rules against the synthetic hospital profile. Verify against the source evidence."
    )
    badge_cols = st.columns(3)
    badge_cols[0].metric("Classification", r["classification"])
    badge_cols[1].metric("Automated relevance", str(r.get("relevance")))
    badge_cols[2].metric("Confidence", r.get("confidence"))
    st.markdown("**Summary**")
    st.write(r.get("summary") or "—")
    st.markdown("**Why it may matter**")
    st.write(r.get("why_it_may_matter") or "—")
    if r.get("relevance_reason"):
        st.markdown("**Relevance reason**")
        st.write(r["relevance_reason"])
    age_min, age_max = r.get("age_min"), r.get("age_max")
    population = (
        "not stated" if age_min is None and age_max is None
        else f"ages {age_min if age_min is not None else '?'}–{age_max if age_max is not None else '?'}"
    )
    st.markdown("**Model-extracted scope** (Qwen, not verified source facts)")
    st.markdown(
        f"- Plan scope: {_listed(r.get('plan_scope'))}\n"
        f"- Population: {population}\n"
        f"- Geography (states): {_listed(r.get('states'))}\n"
        f"- Codes: {_listed(r.get('codes'))}\n"
        f"- Billing setting: {r.get('billing_setting') or 'unknown'} · Service area: {r.get('service_area') or 'unknown'}"
    )
    needed = r.get("additional_data_needed") or []
    st.markdown("**Suggested next step**")
    st.write(
        f"Gather before assessing impact: {', '.join(needed)}." if needed
        else "No additional data flagged by the model; confirm the change against the source evidence."
    )
    if r.get("impact_note"):
        st.caption(f"Synthetic claim-volume estimate: {r['impact_note']}")

    # --- 3. Human review: the only part a reviewer edits ---------------------
    st.markdown("#### 3. Human review")
    # A form submits status + note together on one click; without it, the
    # note's commit-on-blur rerun swallows the first Save click.
    with st.form(key=f"mf_form_{fid}", border=False):
        status = st.radio(
            "Review status", REVIEW_STATUSES, index=REVIEW_STATUSES.index(f.review_status),
            key=f"mf_status_{fid}", horizontal=True,
        )
        note = st.text_area("Review note", value=f.review_note, key=f"mf_note_{fid}")
        if f.reviewed_at:
            st.caption(f"Last reviewed at {f.reviewed_at}")
        if st.form_submit_button("Save review", type="primary"):
            save_review(fid, status, note)
            st.rerun()


def render_export_completed_reviews() -> None:
    st.subheader("Export completed reviews")
    rows = completed_reviews(list_findings())
    if not rows:
        st.caption("No reviewed or dismissed findings yet. Pending findings are never exported.")
        return
    st.caption(f"{len(rows)} completed review(s). Pending findings are excluded.")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    csv_col, json_col = st.columns(2)
    csv_col.download_button(
        "Download CSV", data=buf.getvalue(), file_name="live_completed_reviews.csv",
        mime="text/csv", key="export_csv",
    )
    json_col.download_button(
        "Download JSON", data=json.dumps(rows, indent=2), file_name="live_completed_reviews.json",
        mime="application/json", key="export_json",
    )


def render_demo_controls() -> None:
    with st.expander("Demo controls"):
        st.caption(
            "Clears live monitoring state only: source versions, source checks, findings and reviews, "
            "and captured live snapshots under data/monitor/. Simulated priors, the historical review "
            "queue, config, and tests are not touched."
        )
        confirmed = st.checkbox("I understand this clears live monitoring state", key="reset_confirm")
        if st.button("Reset live demo", disabled=not confirmed, key="reset_demo"):
            clear_findings()
            clear_source_state()
            st.session_state.pop("reset_confirm", None)
            st.rerun()


st.set_page_config(page_title="Payer Policy Change Review", layout="wide")

if "reviews" not in st.session_state:
    st.session_state.reviews = {}

queue = load_queue()
required_ids = {r["change_id"] for r in queue if r.get("relevance") in REQUIRED_RELEVANCE}
completed_required = sum(1 for cid in required_ids if is_reviewed(cid))

st.title("Payer Policy Change Review")
tab_live, tab_historical = st.tabs(["Live Policy Monitoring", "Historical Review Queue (static demo)"])

with tab_live:
    render_live_monitoring()

with tab_historical:
    st.caption(
        "Thin review layer over data/review/final_review_queue.json. Does not call an LLM or "
        "modify upstream detection/relevance/impact data."
    )

    # --- Sidebar: filters + progress -------------------------------------------------
    with st.container(border=True):
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
