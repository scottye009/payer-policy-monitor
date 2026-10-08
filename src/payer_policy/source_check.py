"""Persistent source/version checking for monitored UHC policies.

Immutable PDF snapshots live on the filesystem; workflow state (versions
seen, check outcomes) lives in SQLite so it survives Python/Streamlit
restarts. app.py calls check_for_updates() and get_status() directly.

Per policy, the first check initializes from the simulated prior without
touching the network; every later check fetches the configured UHC URL
and compares its SHA256 against the latest known good version.
"""
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from payer_policy.collect import fetch_pdf, sha256_hex

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "sources.yaml"
MONITOR_DIR = ROOT / "data" / "monitor"
DEFAULT_DB_PATH = MONITOR_DIR / "state.db"
DEFAULT_SNAPSHOT_DIR = MONITOR_DIR / "snapshots"

# policy_id -> its simulated prior PDF, the version each policy starts from.
SIMULATED_PRIORS = {
    "mri_ct_site_of_service": ROOT / "data" / "simulated_prior_raw" / "mri_ct_prior.pdf",
    "sleep_studies": ROOT / "data" / "simulated_prior_raw" / "sleep_studies_prior.pdf",
}
MONITORED_POLICY_IDS = tuple(SIMULATED_PRIORS)

INITIALIZED = "initialized"
UNCHANGED = "unchanged"
UPDATED = "updated"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_versions (
    id              INTEGER PRIMARY KEY,
    policy_id       TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    snapshot_path   TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    is_simulated    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (policy_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS source_checks (
    id                   INTEGER PRIMARY KEY,
    policy_id            TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    checked_at           TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('initialized', 'unchanged', 'updated', 'failed')),
    previous_version_id  INTEGER REFERENCES policy_versions(id),
    previous_hash        TEXT,
    current_version_id   INTEGER REFERENCES policy_versions(id),
    current_hash         TEXT,
    error                TEXT
);
"""


@dataclass
class CheckResult:
    policy_id: str
    source_url: str
    checked_at: str
    status: str
    previous_hash: str | None = None
    previous_snapshot_path: str | None = None
    current_hash: str | None = None
    current_snapshot_path: str | None = None
    current_is_simulated: bool | None = None
    error: str | None = None


def check_for_updates(
    policy_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    fetch_fn: Callable[[str], bytes] = fetch_pdf,
) -> CheckResult:
    """Run one source check for `policy_id`, record it, and return its outcome."""
    source_url = _configured_url(policy_id)
    with closing(_connect(db_path)) as conn:
        previous = _latest_good_version(conn, policy_id)
        if previous is None:
            _initialize_from_simulated_prior(conn, policy_id, source_url)
        else:
            _check_live_source(conn, policy_id, source_url, previous, snapshot_dir, fetch_fn)
    return get_status(policy_id, db_path=db_path)


def get_status(policy_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> CheckResult | None:
    """Most recent source check for `policy_id`, or None if it was never checked."""
    _configured_url(policy_id)
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT c.*,
                   pv.snapshot_path AS previous_snapshot_path,
                   cv.snapshot_path AS current_snapshot_path,
                   cv.is_simulated  AS current_is_simulated
            FROM source_checks c
            LEFT JOIN policy_versions pv ON pv.id = c.previous_version_id
            LEFT JOIN policy_versions cv ON cv.id = c.current_version_id
            WHERE c.policy_id = ?
            ORDER BY c.id DESC
            LIMIT 1
            """,
            (policy_id,),
        ).fetchone()
    if row is None:
        return None
    return CheckResult(
        policy_id=row["policy_id"],
        source_url=row["source_url"],
        checked_at=row["checked_at"],
        status=row["status"],
        previous_hash=row["previous_hash"],
        previous_snapshot_path=row["previous_snapshot_path"],
        current_hash=row["current_hash"],
        current_snapshot_path=row["current_snapshot_path"],
        current_is_simulated=None if row["current_is_simulated"] is None else bool(row["current_is_simulated"]),
        error=row["error"],
    )


def _initialize_from_simulated_prior(conn: sqlite3.Connection, policy_id: str, source_url: str) -> None:
    prior_path = SIMULATED_PRIORS[policy_id]
    try:
        content = prior_path.read_bytes()
    except OSError as exc:
        _record_check(conn, policy_id, source_url, FAILED, error=f"simulated prior unreadable: {exc}")
        return
    # source_url is the UHC URL this simulated version stands in for; it was
    # never downloaded from there, which is_simulated makes explicit.
    current = _get_or_add_version(
        conn, policy_id, sha256_hex(content), source_url, _stored_path(prior_path), is_simulated=True
    )
    _record_check(conn, policy_id, source_url, INITIALIZED, current=current)


def _check_live_source(
    conn: sqlite3.Connection,
    policy_id: str,
    source_url: str,
    previous: sqlite3.Row,
    snapshot_dir: Path,
    fetch_fn: Callable[[str], bytes],
) -> None:
    try:
        content = fetch_fn(source_url)
    except Exception as exc:  # network, HTTP, or validation failure
        _record_check(conn, policy_id, source_url, FAILED, previous=previous, error=str(exc))
        return

    content_hash = sha256_hex(content)
    if content_hash == previous["content_sha256"]:
        _record_check(conn, policy_id, source_url, UNCHANGED, previous=previous, current=previous)
        return

    snapshot_path = _write_snapshot(snapshot_dir, policy_id, content_hash, content)
    current = _get_or_add_version(conn, policy_id, content_hash, source_url, snapshot_path, is_simulated=False)
    _record_check(conn, policy_id, source_url, UPDATED, previous=previous, current=current)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _configured_url(policy_id: str) -> str:
    if policy_id not in SIMULATED_PRIORS:
        raise ValueError(f"unknown monitored policy: {policy_id!r}")
    with CONFIG_PATH.open() as f:
        documents = yaml.safe_load(f)["documents"]
    return next(d["source_url"] for d in documents if d["id"] == policy_id)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stored_path(path: Path) -> str:
    """Repo-relative when inside the repo (the normal case), else absolute."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def _latest_good_version(conn: sqlite3.Connection, policy_id: str) -> sqlite3.Row | None:
    """Version produced by the most recent successful check. Failed checks
    never move this, so a bad fetch can't displace the last good version."""
    return conn.execute(
        """
        SELECT v.* FROM source_checks c
        JOIN policy_versions v ON v.id = c.current_version_id
        WHERE c.policy_id = ? AND c.status != 'failed'
        ORDER BY c.id DESC
        LIMIT 1
        """,
        (policy_id,),
    ).fetchone()


def _write_snapshot(snapshot_dir: Path, policy_id: str, content_hash: str, content: bytes) -> str:
    """Content-addressed and write-once: an existing snapshot is never rewritten."""
    path = snapshot_dir / policy_id / f"{content_hash}.pdf"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".pdf.tmp")
        tmp_path.write_bytes(content)
        os.replace(tmp_path, path)
    return _stored_path(path)


def _get_or_add_version(
    conn: sqlite3.Connection,
    policy_id: str,
    content_hash: str,
    source_url: str,
    snapshot_path: str,
    *,
    is_simulated: bool,
) -> sqlite3.Row:
    # A hash seen before (e.g. a document that reverted) reuses its row.
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO policy_versions
                (policy_id, content_sha256, source_url, snapshot_path, retrieved_at, is_simulated)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (policy_id, content_hash, source_url, snapshot_path, _now(), int(is_simulated)),
        )
    return conn.execute(
        "SELECT * FROM policy_versions WHERE policy_id = ? AND content_sha256 = ?",
        (policy_id, content_hash),
    ).fetchone()


def _record_check(
    conn: sqlite3.Connection,
    policy_id: str,
    source_url: str,
    status: str,
    *,
    previous: sqlite3.Row | None = None,
    current: sqlite3.Row | None = None,
    error: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO source_checks
                (policy_id, source_url, checked_at, status,
                 previous_version_id, previous_hash, current_version_id, current_hash, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                source_url,
                _now(),
                status,
                previous["id"] if previous else None,
                previous["content_sha256"] if previous else None,
                current["id"] if current else None,
                current["content_sha256"] if current else None,
                error,
            ),
        )
