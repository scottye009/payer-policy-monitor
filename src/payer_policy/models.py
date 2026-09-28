from datetime import datetime
from pydantic import BaseModel


class PageText(BaseModel):
    page_number: int
    text: str


class PolicyDocument(BaseModel):
    document_id: str
    title: str
    payer: str
    document_type: str
    source_url: str

    publication_date: str | None = None
    revision_date: str | None = None
    effective_date: str | None = None
    policy_number: str | None = None

    retrieved_at: datetime
    content_sha256: str
    pages: list[PageText]