import hashlib
from pathlib import Path

import requests

PDF_MAGIC = b"%PDF-"
DEFAULT_TIMEOUT = 30.0


class CollectionError(Exception):
    """Raised when a document cannot be retrieved or is not a valid PDF."""


def fetch_pdf(url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Download a document and return its exact raw bytes."""
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise CollectionError(f"request failed for {url}: {exc}") from exc

    if response.status_code != 200:
        raise CollectionError(f"non-200 response ({response.status_code}) for {url}")

    content = response.content
    if not content.startswith(PDF_MAGIC):
        raise CollectionError(f"response body from {url} is not a valid PDF (missing %PDF header)")

    return content


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_raw_pdf(raw_dir: Path, document_id: str, content: bytes) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{document_id}.pdf"
    path.write_bytes(content)
    return path
