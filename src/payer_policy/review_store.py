"""Persistent findings and human review state, in the same SQLite state.db
as source_check.

A finding is one pipeline record for one analyzed version pair. Its
pipeline output (source evidence + automated interpretation) is written
once and never updated; its review columns are only ever written by
save_review(). So re-analysis can neither duplicate a finding nor reset a
reviewer's decision.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from payer_policy.source_check import DEFAULT_DB_PATH

REVIEW_STATUSES = ("pending", "reviewed", "dismissed")

# Source-side fields only. LLM-generated and derived fields (summary,
# classification, relevance, confidence, ...) and the order-dependent
# change_id are deliberately excluded, so identity never depends on model
# output or candidate ordering.
_FINGERPRINT_FIELDS = ("change_type", "before_text", "after_text", "section", "prior_page", "current_page")

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    finding_id     TEXT PRIMARY KEY,
    policy_id      TEXT NOT NULL,
    previous_hash  TEXT NOT NULL,
    current_hash   TEXT NOT NULL,
    -- Source evidence + automated interpretation: written once, never updated.
    record_json    TEXT NOT NULL,
    detected_at    TEXT NOT NULL,
    -- Human review: only written by save_review().
    review_status  TEXT NOT NULL DEFAULT 'pending'
                   CHECK (review_status IN ('pending', 'reviewed', 'dismissed')),
    review_note    TEXT NOT NULL DEFAULT '',
    reviewed_at    TEXT
);

CREATE INDEX IF NOT EXISTS findings_by_pair ON findings (policy_id, previous_hash, current_hash);
"""


@dataclass
class Finding:
    finding_id: str
    policy_id: str
    previous_hash: str
    current_hash: str
    record: dict
    detected_at: str
    review_status: str
    review_note: str
    reviewed_at: str | None


def finding_id(policy_id: str, previous_hash: str, current_hash: str, record: dict) -> str:
    """Deterministic SHA256 over the version pair and the source-side change.
    A JSON list keeps fields from running together and None distinct from ""."""
    key = [policy_id, previous_hash, current_hash, *(record.get(field) for field in _FINGERPRINT_FIELDS)]
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_findings(
    policy_id: str,
    previous_hash: str,
    current_hash: str,
    findings: list[dict],
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    """Insert any findings not already stored; return how many were new.
    Existing findings (and their review state) are left untouched."""
    detected_at = _now()
    with closing(_connect(db_path)) as conn, conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO findings (finding_id, policy_id, previous_hash, current_hash, record_json, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (finding_id) DO NOTHING
            """,
            [
                (
                    finding_id(policy_id, previous_hash, current_hash, record),
                    policy_id,
                    previous_hash,
                    current_hash,
                    json.dumps(record),
                    detected_at,
                )
                for record in findings
            ],
        )
        return conn.total_changes - before


def list_findings(policy_id: str | None = None, *, db_path: Path = DEFAULT_DB_PATH) -> list[Finding]:
    """Stored findings in the order they were first saved."""
    query = "SELECT * FROM findings"
    params: tuple = ()
    if policy_id is not None:
        query += " WHERE policy_id = ?"
        params = (policy_id,)
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(query + " ORDER BY rowid", params).fetchall()
    return [
        Finding(
            finding_id=row["finding_id"],
            policy_id=row["policy_id"],
            previous_hash=row["previous_hash"],
            current_hash=row["current_hash"],
            record=json.loads(row["record_json"]),
            detected_at=row["detected_at"],
            review_status=row["review_status"],
            review_note=row["review_note"],
            reviewed_at=row["reviewed_at"],
        )
        for row in rows
    ]


def save_review(
    finding_id: str, review_status: str, review_note: str = "", *, db_path: Path = DEFAULT_DB_PATH
) -> None:
    """Record a reviewer decision. reviewed_at is set for reviewed/dismissed
    and cleared when a finding is put back to pending."""
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"review_status must be one of {REVIEW_STATUSES}, got {review_status!r}")
    reviewed_at = None if review_status == "pending" else _now()
    with closing(_connect(db_path)) as conn, conn:
        cursor = conn.execute(
            "UPDATE findings SET review_status = ?, review_note = ?, reviewed_at = ? WHERE finding_id = ?",
            (review_status, review_note, reviewed_at, finding_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no finding with id {finding_id}")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
