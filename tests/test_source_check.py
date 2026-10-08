import sqlite3
from unittest.mock import Mock, patch

import pytest

from payer_policy import source_check
from payer_policy.collect import CollectionError, fetch_pdf, sha256_hex
from payer_policy.source_check import check_for_updates, get_status

POLICY_ID = "mri_ct_site_of_service"
LIVE_V1 = b"%PDF-1.7 live version one"
LIVE_V2 = b"%PDF-1.7 live version two"


@pytest.fixture
def paths(tmp_path):
    return {"db_path": tmp_path / "state.db", "snapshot_dir": tmp_path / "snapshots"}


def _serve(content: bytes):
    return lambda url: content


def _fail(url: str) -> bytes:
    raise CollectionError(f"request failed for {url}: connection reset")


def _no_network(url: str) -> bytes:
    raise AssertionError("initialization must not hit the network")


def _rows(db_path, table):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _snapshot_files(paths):
    return sorted((paths["snapshot_dir"] / POLICY_ID).glob("*"))


def test_first_check_initializes_from_simulated_prior_without_network(paths):
    result = check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)

    prior_bytes = source_check.BASELINES[POLICY_ID].path.read_bytes()
    assert result.status == "initialized"
    assert result.current_hash == sha256_hex(prior_bytes)
    assert result.previous_hash is None
    assert result.current_snapshot_path == "data/simulated_prior_raw/mri_ct_prior.pdf"
    assert result.current_is_simulated is True

    [version] = _rows(paths["db_path"], "policy_versions")
    assert version["is_simulated"] == 1
    assert version["source_url"] == source_check._configured_url(POLICY_ID)
    assert version["source_url"].startswith("https://www.uhcprovider.com/")
    assert _snapshot_files(paths) == []


def test_initialized_then_updated_then_unchanged(paths):
    initialized = check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    updated = check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    unchanged = check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)

    assert updated.status == "updated"
    assert updated.previous_hash == initialized.current_hash
    assert updated.current_hash == sha256_hex(LIVE_V1)
    assert updated.current_is_simulated is False
    assert updated.previous_snapshot_path == initialized.current_snapshot_path

    assert unchanged.status == "unchanged"
    assert unchanged.previous_hash == unchanged.current_hash == sha256_hex(LIVE_V1)

    # Same live PDF twice: one live version, one snapshot file.
    assert len(_rows(paths["db_path"], "policy_versions")) == 2
    [snapshot] = _snapshot_files(paths)
    assert snapshot.name == f"{sha256_hex(LIVE_V1)}.pdf"
    assert snapshot.read_bytes() == LIVE_V1
    assert [r["status"] for r in _rows(paths["db_path"], "source_checks")] == [
        "initialized", "updated", "unchanged",
    ]


def test_document_changing_at_same_url_is_updated(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    result = check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V2), **paths)

    assert result.status == "updated"
    assert result.previous_hash == sha256_hex(LIVE_V1)
    assert result.current_hash == sha256_hex(LIVE_V2)
    assert len(_snapshot_files(paths)) == 2


def test_failed_fetch_preserves_last_good_version(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    failed = check_for_updates(POLICY_ID, fetch_fn=_fail, **paths)

    assert failed.status == "failed"
    assert "connection reset" in failed.error
    assert failed.previous_hash == sha256_hex(LIVE_V1)
    assert failed.current_hash is None
    assert len(_rows(paths["db_path"], "policy_versions")) == 2

    # The next good fetch compares against the last good version, not the failure.
    recovered = check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    assert recovered.status == "unchanged"


def test_empty_response_fails_through_existing_validation(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    with patch("payer_policy.collect.requests.get", return_value=Mock(status_code=200, content=b"")):
        result = check_for_updates(POLICY_ID, fetch_fn=fetch_pdf, **paths)

    assert result.status == "failed"
    assert "not a valid PDF" in result.error
    assert result.previous_hash == sha256_hex(source_check.BASELINES[POLICY_ID].path.read_bytes())


def test_reverted_document_reuses_existing_version(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V2), **paths)
    result = check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)

    assert result.status == "updated"
    assert result.current_hash == sha256_hex(LIVE_V1)
    assert len(_rows(paths["db_path"], "policy_versions")) == 3


def test_state_is_read_back_from_disk(paths):
    assert get_status(POLICY_ID, db_path=paths["db_path"]) is None
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)

    # Every call opens a fresh connection, so this is what a restarted app sees.
    status = get_status(POLICY_ID, db_path=paths["db_path"])
    assert status.status == "updated"
    assert status.current_hash == sha256_hex(LIVE_V1)

    # A restarted app must not re-initialize from the simulated prior.
    assert check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths).status == "unchanged"


def test_policies_are_tracked_independently(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)

    assert check_for_updates("sleep_studies", fetch_fn=_no_network, **paths).status == "initialized"
    assert get_status(POLICY_ID, db_path=paths["db_path"]).status == "updated"


def test_unknown_policy_is_rejected(paths):
    with pytest.raises(ValueError):
        check_for_updates("allergen_testing", fetch_fn=_no_network, **paths)


def test_clear_source_state_resets_to_simulated_baseline(paths):
    check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths)
    check_for_updates(POLICY_ID, fetch_fn=_serve(LIVE_V1), **paths)
    prior_pdf = source_check.BASELINES[POLICY_ID].path
    prior_bytes = prior_pdf.read_bytes()

    source_check.clear_source_state(**paths)

    assert get_status(POLICY_ID, db_path=paths["db_path"]) is None
    assert _rows(paths["db_path"], "policy_versions") == []
    assert not paths["snapshot_dir"].exists()
    # The simulated prior lives outside the snapshot dir and is untouched.
    assert prior_pdf.read_bytes() == prior_bytes
    assert check_for_updates(POLICY_ID, fetch_fn=_no_network, **paths).status == "initialized"


def test_real_local_prior_initializes_as_not_simulated(paths):
    policy_id = "surgery_elbow"
    baseline = source_check.BASELINES[policy_id]
    assert baseline.is_simulated is False

    result = check_for_updates(policy_id, fetch_fn=_no_network, **paths)

    assert result.status == "initialized"
    assert result.current_hash == sha256_hex(baseline.path.read_bytes())
    assert result.current_is_simulated is False
    assert result.current_origin == "local_prior"
    [version] = _rows(paths["db_path"], "policy_versions")
    assert version["is_simulated"] == 0
    assert version["source_url"] == source_check._configured_url(policy_id)
    assert version["snapshot_path"].startswith("data/prior/")


def test_live_update_after_real_local_prior(paths):
    policy_id = "home_health_care"
    check_for_updates(policy_id, fetch_fn=_no_network, **paths)
    result = check_for_updates(policy_id, fetch_fn=_serve(LIVE_V1), **paths)

    assert result.status == "updated"
    assert result.current_origin == "live_fetch"
    previous = source_check.get_version_by_hash(policy_id, result.previous_hash, db_path=paths["db_path"])
    current = source_check.get_version_by_hash(policy_id, result.current_hash, db_path=paths["db_path"])
    assert previous.origin == "local_prior" and previous.is_simulated is False
    assert current.origin == "live_fetch"


def test_all_monitored_policies_have_baseline_and_url():
    assert set(source_check.MONITORED_POLICY_IDS) == {
        "mri_ct_site_of_service", "sleep_studies", "surgery_elbow", "home_health_care", "spinraza",
    }
    for policy_id in source_check.MONITORED_POLICY_IDS:
        assert source_check.BASELINES[policy_id].path.exists()
        assert source_check._configured_url(policy_id).startswith("https://www.uhcprovider.com/")


def test_spinraza_failure_test_initializes_then_fails_keeping_prior(paths):
    # Initializes from the simulated prior; a live fetch that returns HTML
    # (what UHC serves for the deliberately broken URL) fails validation.
    init = check_for_updates("spinraza", fetch_fn=_no_network, **paths)
    assert init.status == "initialized" and init.current_origin == "simulated_prior"

    with patch("payer_policy.collect.requests.get",
               return_value=Mock(status_code=200, content=b"<html>Page not found</html>")):
        failed = check_for_updates("spinraza", fetch_fn=fetch_pdf, **paths)

    assert failed.status == "failed"
    assert "not a valid PDF" in failed.error
    assert failed.previous_hash == init.current_hash
