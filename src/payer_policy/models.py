from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PageText(BaseModel):
    page_number: int
    text: str


class SourceConfig(BaseModel):
    """Manually curated source metadata from config/sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    document_type: str
    source_url: str
    role: str | None = None
    index: str | None = None
    # References another configured document's id: an official UHC bulletin
    # to consult for this document's effective date if its own PDF has none.
    effective_date_bulletin: str | None = None


class IndexSource(BaseModel):
    """A configured UHC policy-family index page listing document links."""

    model_config = ConfigDict(extra="forbid")

    url: str


class ExtractedMetadata(BaseModel):
    """Metadata read from the PDF's own text."""

    policy_number: str | None = None
    publication_date: str | None = None
    revision_date: str | None = None
    effective_date: str | None = None


class IndexMetadata(BaseModel):
    """Metadata read from the document's configured index page.

    Kept distinct from ExtractedMetadata: UHC's "Last Published" date is its
    own concept and is never mapped onto publication_date.
    """

    last_published_date: str | None = None
    index_url: str | None = None
    index_observed_at: datetime | None = None


class EffectiveDateProvenance(BaseModel):
    """Records where effective_date came from when it wasn't found in the
    policy PDF itself, e.g. resolved from a linked UHC bulletin's default or
    state-specific exception date for the organization's geography."""

    source: str
    bulletin_document_id: str
    geography: str
    used_state_exception: bool


class PolicyDocument(BaseModel):
    document_id: str
    title: str
    payer: str
    document_type: str
    # Optional: a local simulated artifact has no payer URL to record, and
    # inventing one would misrepresent it as a retrieved payer document.
    source_url: str | None = None

    publication_date: str | None = None
    revision_date: str | None = None
    effective_date: str | None = None
    effective_date_provenance: EffectiveDateProvenance | None = None
    policy_number: str | None = None

    last_published_date: str | None = None
    index_url: str | None = None
    index_observed_at: datetime | None = None

    # Optional for the same reason as source_url: a local artifact was never
    # retrieved from a remote source, so there is no retrieval to time.
    retrieved_at: datetime | None = None
    content_sha256: str
    local_path: str
    pages: list[PageText]

    # Set only for non-payer artifacts (e.g. simulated prior versions) so
    # they're never mistaken for a real retrieved payer document.
    is_simulated: bool = False
    artifact_type: str | None = None
    source_path: str | None = None


class LLMAdjudication(BaseModel):
    """Structured output required from the change-adjudication LLM call."""

    classification: Literal["substantive", "non_substantive", "uncertain"]
    summary: str
    reason: str
    changed_dimensions: list[str] = []
    confidence: float


class ChangeRecord(BaseModel):
    """One prior/current passage-level candidate change, fully auditable
    without reopening the source PDFs."""

    change_id: str
    document_id: str
    prior_policy_number: str | None
    current_policy_number: str | None
    prior_is_simulated: bool

    change_type: Literal["added", "removed", "modified"]
    # Exact source substrings from the processed JSON -- never paraphrased.
    before_text: str | None
    after_text: str | None
    section: str | None
    prior_page: int | None
    current_page: int | None

    semantic_similarity: float | None
    revision_history_match: bool
    revision_history_evidence: str | None

    classification: Literal["substantive", "non_substantive", "uncertain"]
    changed_dimensions: list[str] = []
    summary: str
    reason: str
    confidence: float

    review_status: str = "pending"
