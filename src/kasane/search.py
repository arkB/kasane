import math
import logging
from datetime import datetime

from .embedder import encode
from .storage import MemoryResult, fts_search, get_memories_by_ids, vec_search

RRF_K = 60
TIME_DECAY_HALF_LIFE = 30.0
logger = logging.getLogger(__name__)


def rrf_score(rank: int, k: int = RRF_K) -> float:
    return 1.0 / (k + rank)


def time_decay(days_old: float, half_life: float = TIME_DECAY_HALF_LIFE) -> float:
    return math.pow(0.5, days_old / half_life)


def hybrid_search(query: str, top_k: int = 5) -> list[MemoryResult]:
    fts_results = fts_search(query, limit=50)
    vec_results: list[tuple[int, float]] = []
    try:
        query_embedding = encode(query, prefix="query")
        if isinstance(query_embedding[0], list):
            query_embedding = query_embedding[0]
        vec_results = vec_search(query_embedding, limit=50)
    except Exception as e:
        logger.warning(f"Vector search unavailable, falling back to FTS-only search: {e}")
    id_scores: dict[int, float] = {}
    for id_, rank in fts_results:
        id_scores[id_] = id_scores.get(id_, 0.0) + rrf_score(rank)
    for id_, distance in vec_results:
        similarity = 1.0 / (1.0 + distance)
        id_scores[id_] = id_scores.get(id_, 0.0) + similarity * 0.1
    if not id_scores:
        return []
    memories = get_memories_by_ids(list(id_scores.keys()))
    scored_results = []
    for id_, base_score in id_scores.items():
        if id_ not in memories:
            continue
        memory = memories[id_]
        now = (
            datetime.now(memory.created_at.tzinfo)
            if memory.created_at.tzinfo is not None
            else datetime.now()
        )
        days_old = (now - memory.created_at).total_seconds() / 86400.0
        decay = time_decay(max(0, days_old))
        final_score = base_score * decay
        memory.score = final_score
        scored_results.append(memory)
    scored_results.sort(key=lambda x: x.score, reverse=True)
    return scored_results[:top_k]
