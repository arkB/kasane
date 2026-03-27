import os
from typing import Literal
import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = "cl-nagoya/ruri-v3-310m"
_model: SentenceTransformer | None = None


def _load_model(allow_download: bool) -> SentenceTransformer:
    if allow_download:
        return SentenceTransformer(MODEL_NAME)
    original_hf_offline = os.environ.get("HF_HUB_OFFLINE")
    original_transformers_offline = os.environ.get("TRANSFORMERS_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        return SentenceTransformer(MODEL_NAME, local_files_only=True)
    finally:
        if original_hf_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = original_hf_offline
        if original_transformers_offline is None:
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        else:
            os.environ["TRANSFORMERS_OFFLINE"] = original_transformers_offline


def get_model(allow_download: bool = False) -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info(f"Loading model {MODEL_NAME}...")
        try:
            _model = _load_model(allow_download=allow_download)
            logger.info("Loaded model from local cache")
        except Exception as e:
            if not allow_download:
                raise RuntimeError(
                    "Embedding model is not available in local cache. "
                    "Run `uv run python -m kasane.main warmup` once before offline use."
                ) from e
            _model = _load_model(allow_download=True)
        logger.info("Model loaded")
    return _model


def encode(
    texts: str | list[str],
    prefix: Literal["query"] | Literal["passage"] = "passage",
) -> list[list[float]] | list[float]:
    model = get_model()
    if isinstance(texts, str):
        prefixed_text = f"{prefix}: {texts}"
        embedding = model.encode(prefixed_text, normalize_embeddings=True)
        return embedding.tolist()
    prefixed_texts = [f"{prefix}: {t}" for t in texts]
    embeddings = model.encode(prefixed_texts, normalize_embeddings=True)
    return embeddings.tolist()


def warmup() -> None:
    logger.info(f"Warming up model {MODEL_NAME}...")
    global _model
    _model = None
    model = get_model(allow_download=True)
    _ = model.encode("warmup", normalize_embeddings=True)
    logger.info("Warmup complete")
