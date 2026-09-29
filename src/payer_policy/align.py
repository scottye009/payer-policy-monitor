import os

DEFAULT_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"

_model = None
_model_name = None


def _get_model(model_name: str | None = None):
    global _model, _model_name
    model_name = model_name or os.environ.get("CHANGE_ALIGN_MODEL", DEFAULT_MODEL_NAME)
    if _model is None or _model_name != model_name:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(model_name)
        _model_name = model_name
    return _model


def embed(texts: list[str], *, model_name: str | None = None) -> list[list[float]]:
    """Return a normalized embedding per text, using MPNet only to align
    corresponding passages -- never to decide whether a change is
    substantive."""
    if not texts:
        return []
    model = _get_model(model_name)
    vectors = model.encode(list(texts), normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    # Vectors from embed() are already normalized, so dot product == cosine.
    return float(sum(x * y for x, y in zip(a, b)))
