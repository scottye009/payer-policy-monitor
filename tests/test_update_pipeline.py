import json
from pathlib import Path

import pytest

import update_pipeline
from payer_policy.models import LLMAdjudication
from payer_policy.source_check import CheckResult, check_for_updates
from update_pipeline import run_update_pipeline

ROOT = Path(__file__).resolve().parent.parent
POLICY_ID = "mri_ct_site_of_service"
CONFIG_TITLE = "Magnetic Resonance Imaging (MRI) and Computed Tomography (CT) Scan - Site of Service"
# The take-home's live MRI/CT PDF; byte-identical to what UHC serves today.
LIVE_PDF = ROOT / "data" / "raw" / f"{POLICY_ID}.pdf"


def _fake_adjudicate(before_text, after_text, section, revision_history_evidence, **kwargs):
    return LLMAdjudication(
        classification="substantive",
        summary="test",
        reason="test",
        confidence=0.9,
        billing_setting="hospital_outpatient",
        service_area="imaging",
    )


def _no_adjudicate(**kwargs):
    raise AssertionError("adjudication must not run")


@pytest.fixture
def updated_check(tmp_path):
    """Simulated baseline -> real live PDF, recorded in a throwaway monitor DB."""
    paths = {"db_path": tmp_path / "state.db", "snapshot_dir": tmp_path / "snapshots"}
    check_for_updates(POLICY_ID, **paths)
    result = check_for_updates(POLICY_ID, fetch_fn=lambda url: LIVE_PDF.read_bytes(), **paths)
    assert result.status == "updated"
    return result, paths["db_path"]


@pytest.mark.parametrize("status", ["initialized", "unchanged", "failed"])
def test_non_updated_checks_do_nothing(status):
    check = CheckResult(policy_id=POLICY_ID, source_url="u", checked_at="t", status=status)
    assert run_update_pipeline(POLICY_ID, check, adjudicate_fn=_no_adjudicate) is None
    assert run_update_pipeline(POLICY_ID, None, adjudicate_fn=_no_adjudicate) is None


def test_both_documents_use_config_identity_and_simulated_flag(updated_check, monkeypatch):
    check, db_path = updated_check
    captured = {}

    def fake_detect_changes(prior, current, *, adjudicate_fn):
        captured.update(prior=prior, current=current)
        return []

    monkeypatch.setattr(update_pipeline, "detect_changes", fake_detect_changes)
    result = run_update_pipeline(POLICY_ID, check, db_path=db_path)

    prior, current = captured["prior"], captured["current"]
    for doc in (prior, current):
        assert doc.document_id == POLICY_ID
        assert doc.title == CONFIG_TITLE
        assert doc.document_type == "medical_policy"
        assert doc.source_url.startswith("https://www.uhcprovider.com/")
    assert prior.is_simulated and prior.artifact_type == "simulated_prior" and prior.retrieved_at is None
    assert not current.is_simulated and current.retrieved_at is not None
    assert prior.content_sha256 == check.previous_hash
    assert current.content_sha256 == check.current_hash
    assert prior.pages and current.pages

    assert result.policy_id == POLICY_ID
    assert (result.previous_hash, result.current_hash) == (check.previous_hash, check.current_hash)
    assert result.previous_is_simulated is True
    assert result.findings == []


def test_missing_version_is_an_error(updated_check):
    check, db_path = updated_check
    check.current_hash = "0" * 64
    with pytest.raises(LookupError):
        run_update_pipeline(POLICY_ID, check, db_path=db_path, adjudicate_fn=_no_adjudicate)


def test_end_to_end_matches_take_home_candidates(updated_check):
    """Real extraction + MPNet + review processing; only Qwen is faked. The
    version pair is the same one the take-home analyzed, so the candidate
    passages must match the committed results exactly."""
    pytest.importorskip("sentence_transformers")
    check, db_path = updated_check

    result = run_update_pipeline(POLICY_ID, check, db_path=db_path, adjudicate_fn=_fake_adjudicate)

    committed = json.loads((ROOT / "data" / "review" / f"{POLICY_ID}_changes.json").read_text())
    passage_keys = ("change_id", "change_type", "before_text", "after_text", "section", "prior_page", "current_page")
    assert [{k: f[k] for k in passage_keys} for f in result.findings] == [
        {k: c[k] for k in passage_keys} for c in committed
    ]
    for finding in result.findings:
        assert finding["prior_is_simulated"] is True
        assert finding["relevance"] in ("relevant", "needs_investigation", "irrelevant")
        assert "potential_annual_claim_lines" in finding


def test_real_local_prior_document_is_not_marked_simulated(tmp_path, monkeypatch):
    policy_id = "surgery_elbow"
    paths = {"db_path": tmp_path / "state.db", "snapshot_dir": tmp_path / "snapshots"}
    check_for_updates(policy_id, **paths)
    check = check_for_updates(policy_id, fetch_fn=lambda url: b"%PDF-1.7 placeholder live", **paths)
    captured = {}
    monkeypatch.setattr(
        update_pipeline, "build_policy_document",
        lambda source, payer, version: captured.setdefault(version.origin, (source, version)) and None,
    )
    monkeypatch.setattr(update_pipeline, "detect_changes", lambda prior, current, *, adjudicate_fn: [])

    result = run_update_pipeline(policy_id, check, db_path=paths["db_path"])

    assert result.previous_is_simulated is False
    source, version = captured["local_prior"]
    assert source.title == "Surgery of the Elbow"
    assert version.snapshot_path.startswith("data/prior/")


def test_build_policy_document_for_real_local_prior(tmp_path):
    policy_id = "home_health_care"
    paths = {"db_path": tmp_path / "state.db", "snapshot_dir": tmp_path / "snapshots"}
    check = check_for_updates(policy_id, **paths)
    version = update_pipeline.get_version_by_hash(policy_id, check.current_hash, db_path=paths["db_path"])
    payer, _g, _i, sources = update_pipeline.load_sources(update_pipeline.CONFIG_PATH)
    source = next(s for s in sources if s.id == policy_id)

    doc = update_pipeline.build_policy_document(source, payer, version)

    assert doc.is_simulated is False
    assert doc.artifact_type == "local_prior"
    assert doc.retrieved_at is None
    assert doc.policy_number == "MP.022.27"
    assert doc.title == "Home Health, Skilled, and Custodial Care Services"
