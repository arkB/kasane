from typing import Literal
import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

MODEL_NAME = "cl-nagoya/ruri-v3-310m"
_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info(f"Loading model {MODEL_NAME}...")
        try:
            _model = SentenceTransformer(MODEL_NAME, local_files_only=True)
            logger.info("Loaded model from local cache")
        except Exception:
            _model = SentenceTransformer(MODEL_NAME)
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
    model = get_model()
    _ = model.encode("warmup", normalize_embeddings=True)
    logger.info("Warmup complete")
