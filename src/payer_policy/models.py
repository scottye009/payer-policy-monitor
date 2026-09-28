from datetime import datetime

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
    source_url: str

    publication_date: str | None = None
    revision_date: str | None = None
    effective_date: str | None = None
    effective_date_provenance: EffectiveDateProvenance | None = None
    policy_number: str | None = None

    last_published_date: str | None = None
    index_url: str | None = None
    index_observed_at: datetime | None = None

    retrieved_at: datetime
    content_sha256: str
    local_path: str
    pages: list[PageText]
