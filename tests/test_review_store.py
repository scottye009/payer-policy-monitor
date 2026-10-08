import sqlite3

import pytest

from payer_policy.review_store import finding_id, list_findings, save_findings, save_review
from payer_policy.source_check import check_for_updates, get_status

POLICY_ID = "mri_ct_site_of_service"
HASH_A, HASH_B, HASH_C = "a" * 64, "b" * 64, "c" * 64


def _record(n: int, **overrides) -> dict:
    record = {
        "change_id": f"{POLICY_ID}-{n:04d}",
        "change_type": "modified",
        "before_text": f"Coverage is limited to patients under {n} years of age.",
        "after_text": f"Coverage is limited to patients under {n + 2} years of age.",
        "section": "Coverage Rationale",
        "prior_page": 2,
        "current_page": 2,
        "classification": "substantive",
        "summary": "Age limit raised.",
        "relevance": "relevant",
        "confidence": 0.9,
    }
    record.update(overrides)
    return record


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


def _ids(db_path, **kwargs):
    return [f.finding_id for f in list_findings(db_path=db_path, **kwargs)]


def test_finding_id_ignores_llm_and_derived_fields():
    base = _record(16)
    reworded = _record(
        16, change_id="other-9999", classification="non_substantive", summary="Different",
        relevance="irrelevant", confidence=0.1, why_it_may_matter="x",
    )
    assert finding_id(POLICY_ID, HASH_A, HASH_B, base) == finding_id(POLICY_ID, HASH_A, HASH_B, reworded)


@pytest.mark.parametrize(
    "field, value",
    [("change_type", "added"), ("before_text", None), ("after_text", "x"),
     ("section", "Applicable Codes"), ("prior_page", 3), ("current_page", 3)],
)
def test_finding_id_changes_with_each_source_field(field, value):
    assert finding_id(POLICY_ID, HASH_A, HASH_B, _record(16)) != finding_id(
        POLICY_ID, HASH_A, HASH_B, _record(16, **{field: value})
    )


def test_finding_id_changes_with_version_pair_and_policy():
    base = finding_id(POLICY_ID, HASH_A, HASH_B, _record(16))
    assert base != finding_id(POLICY_ID, HASH_B, HASH_C, _record(16))
    assert base != finding_id(POLICY_ID, HASH_A, HASH_C, _record(16))
    assert base != finding_id("sleep_studies", HASH_A, HASH_B, _record(16))


def test_rerun_of_same_pair_creates_no_duplicates(db_path):
    records = [_record(n) for n in range(3)]
    assert save_findings(POLICY_ID, HASH_A, HASH_B, records, db_path=db_path) == 3
    assert save_findings(POLICY_ID, HASH_A, HASH_B, records, db_path=db_path) == 0
    assert len(list_findings(db_path=db_path)) == 3


def test_candidate_order_does_not_affect_identity(db_path):
    records = [_record(n) for n in range(3)]
    save_findings(POLICY_ID, HASH_A, HASH_B, records, db_path=db_path)
    # Reordered candidates also get renumbered change_ids.
    reordered = [dict(r, change_id=f"{POLICY_ID}-{i:04d}") for i, r in enumerate(reversed(records))]
    assert save_findings(POLICY_ID, HASH_A, HASH_B, reordered, db_path=db_path) == 0


def test_identical_source_records_deduplicate(db_path):
    assert save_findings(POLICY_ID, HASH_A, HASH_B, [_record(1), _record(1)], db_path=db_path) == 1


def test_reanalysis_never_resets_review_or_rewrites_interpretation(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(16)], db_path=db_path)
    [finding] = list_findings(db_path=db_path)
    save_review(finding.finding_id, "dismissed", "Admin-only wording change.", db_path=db_path)

    # Same pair re-analyzed; the LLM now answers differently.
    rerun = _record(16, classification="uncertain", summary="Changed answer")
    save_findings(POLICY_ID, HASH_A, HASH_B, [rerun], db_path=db_path)

    [after] = list_findings(db_path=db_path)
    assert after.review_status == "dismissed"
    assert after.review_note == "Admin-only wording change."
    assert after.reviewed_at is not None
    assert after.record["classification"] == "substantive"
    assert after.record["summary"] == "Age limit raised."


def test_review_survives_restart(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(16)], db_path=db_path)
    [finding] = list_findings(db_path=db_path)
    save_review(finding.finding_id, "reviewed", "Escalated to imaging lead.", db_path=db_path)

    # Every call opens a fresh connection; read the raw table too.
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT review_status, review_note FROM findings").fetchone()
    assert row == ("reviewed", "Escalated to imaging lead.")
    assert list_findings(db_path=db_path)[0].review_status == "reviewed"


def test_new_revision_creates_new_pending_finding(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(16)], db_path=db_path)
    [old] = list_findings(db_path=db_path)
    save_review(old.finding_id, "dismissed", "Not relevant.", db_path=db_path)

    # B -> C contains an identical-looking change.
    assert save_findings(POLICY_ID, HASH_B, HASH_C, [_record(16)], db_path=db_path) == 1

    by_pair = {(f.previous_hash, f.current_hash): f for f in list_findings(db_path=db_path)}
    assert by_pair[(HASH_A, HASH_B)].review_status == "dismissed"
    new = by_pair[(HASH_B, HASH_C)]
    assert new.review_status == "pending"
    assert new.review_note == ""
    assert new.reviewed_at is None
    assert new.finding_id != old.finding_id


def test_back_to_pending_clears_reviewed_at(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(16)], db_path=db_path)
    [finding] = list_findings(db_path=db_path)
    save_review(finding.finding_id, "reviewed", "", db_path=db_path)
    save_review(finding.finding_id, "pending", "Reopened.", db_path=db_path)
    [after] = list_findings(db_path=db_path)
    assert (after.review_status, after.review_note, after.reviewed_at) == ("pending", "Reopened.", None)


def test_invalid_review_inputs_are_rejected(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(16)], db_path=db_path)
    [finding] = list_findings(db_path=db_path)
    with pytest.raises(ValueError):
        save_review(finding.finding_id, "approved", db_path=db_path)
    with pytest.raises(KeyError):
        save_review("0" * 64, "reviewed", db_path=db_path)


def test_list_findings_filters_by_policy(db_path):
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(1)], db_path=db_path)
    save_findings("sleep_studies", HASH_A, HASH_B, [_record(1)], db_path=db_path)
    assert len(list_findings(POLICY_ID, db_path=db_path)) == 1
    assert len(list_findings(db_path=db_path)) == 2


def test_shares_state_db_with_source_checks(db_path, tmp_path):
    check_for_updates(POLICY_ID, db_path=db_path, snapshot_dir=tmp_path / "snapshots")
    save_findings(POLICY_ID, HASH_A, HASH_B, [_record(1)], db_path=db_path)
    # A later source check leaves findings and reviews alone.
    check_for_updates(POLICY_ID, db_path=db_path, snapshot_dir=tmp_path / "snapshots",
                      fetch_fn=lambda url: b"%PDF-1.7 live")
    assert get_status(POLICY_ID, db_path=db_path).status == "updated"
    assert len(list_findings(db_path=db_path)) == 1
