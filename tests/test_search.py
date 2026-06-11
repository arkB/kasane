from datetime import UTC, datetime, timedelta

from kasane import search
from kasane.storage import MemoryResult
from kasane.search import rrf_score, time_decay


def test_rrf_score():
    assert rrf_score(1) == 1.0 / 61
    assert rrf_score(0) == 1.0 / 60
    assert rrf_score(10) == 1.0 / 70


def test_time_decay():
    decay_30 = time_decay(30)
    assert 0.49 < decay_30 < 0.51
    decay_60 = time_decay(60)
    assert 0.24 < decay_60 < 0.26
    decay_0 = time_decay(0)
    assert decay_0 == 1.0


def test_hybrid_search_falls_back_to_fts(monkeypatch):
    now = datetime.now()
    memory = MemoryResult(
        id=1,
        session_id="session-1",
        chunk_text="watch-all watcher cpu memory",
        created_at=now - timedelta(days=1),
        score=0.0,
    )
    monkeypatch.setattr(search, "fts_search", lambda _query, limit=50: [(1, 1)])
    monkeypatch.setattr(search, "encode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(search, "vec_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(search, "get_memories_by_ids", lambda _ids: {1: memory})

    results = search.hybrid_search("watch-all", top_k=5)
    assert len(results) == 1
    assert results[0].id == 1


def test_hybrid_search_handles_timezone_aware_created_at(monkeypatch):
    memory = MemoryResult(
        id=1,
        session_id="session-1",
        chunk_text="timezone aware memory",
        created_at=datetime.now(UTC) - timedelta(hours=1),
        score=0.0,
    )
    monkeypatch.setattr(search, "fts_search", lambda _query, limit=50: [(1, 1)])
    monkeypatch.setattr(search, "encode", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(search, "vec_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(search, "get_memories_by_ids", lambda _ids: {1: memory})

    results = search.hybrid_search("timezone", top_k=5)
    assert len(results) == 1
    assert results[0].score > 0.0


def test_hybrid_search_uses_vector_by_default(monkeypatch):
    now = datetime.now()
    memory = MemoryResult(
        id=1,
        session_id="session-1",
        chunk_text="vector memory",
        created_at=now,
        score=0.0,
    )
    calls = {"encode": 0, "vec_search": 0}

    def encode_query(*_args, **_kwargs):
        calls["encode"] += 1
        return [0.1] * 768

    def vector_search(*_args, **_kwargs):
        calls["vec_search"] += 1
        return [(1, 0.2)]

    monkeypatch.setattr(search, "fts_search", lambda _query, limit=50: [])
    monkeypatch.setattr(search, "encode", encode_query)
    monkeypatch.setattr(search, "vec_search", vector_search)
    monkeypatch.setattr(search, "get_memories_by_ids", lambda _ids: {1: memory})

    results = search.hybrid_search("vector", top_k=5)

    assert len(results) == 1
    assert calls == {"encode": 1, "vec_search": 1}


def test_hybrid_search_no_vector_skips_embedding_and_vector_search(monkeypatch):
    now = datetime.now()
    memory = MemoryResult(
        id=1,
        session_id="session-1",
        chunk_text="fts only memory",
        created_at=now,
        score=0.0,
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("vector path should not be used")

    monkeypatch.setattr(search, "fts_search", lambda _query, limit=50: [(1, 1)])
    monkeypatch.setattr(search, "encode", fail_if_called)
    monkeypatch.setattr(search, "vec_search", fail_if_called)
    monkeypatch.setattr(search, "get_memories_by_ids", lambda _ids: {1: memory})

    results = search.hybrid_search("fts", top_k=5, use_vector=False)

    assert len(results) == 1
    assert results[0].id == 1
