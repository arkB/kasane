import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from kasane import storage
from kasane.storage import (
    get_import_state,
    MemoryChunk,
    delete_session,
    get_session_import_info,
    init_db,
    insert_chunks,
    normalize_fts_query,
    session_exists,
    set_import_state,
)


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr(storage, "DB_PATH", db_path)
        init_db()
        yield db_path


def _get_connection_with_vec(db_path):
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def test_init_db_creates_tables(temp_db):
    import sqlite3

    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    assert "memories" in tables
    assert "memories_fts" in tables
    assert "memories_vec" in tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    triggers = {row[0] for row in cursor.fetchall()}
    assert "memories_ai" in triggers
    assert "memories_ad" in triggers
    assert "memories_au" in triggers
    conn.close()


def test_insert_chunks_and_sync_ids(temp_db):
    chunks = [
        MemoryChunk(
            session_id="test123",
            chunk_text="Q: Test?\nA: Answer.",
            created_at=datetime(2025, 1, 1, 12, 0, 0),
            metadata={"transcript_path": "/tmp/test.jsonl", "chunk_index": 0},
        )
    ]
    embeddings = [[0.1] * 768]
    insert_chunks(chunks, embeddings)
    conn = _get_connection_with_vec(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT id, session_id, chunk_text, created_at FROM memories")
    row = cursor.fetchone()
    assert row is not None
    memory_id = row[0]
    assert row[1] == "test123"
    assert row[2] == "Q: Test?\nA: Answer."
    cursor.execute("SELECT id FROM memories_vec WHERE id = ?", (memory_id,))
    vec_row = cursor.fetchone()
    assert vec_row is not None
    conn.close()


def test_session_exists(temp_db):
    assert not session_exists("nonexistent")
    chunks = [
        MemoryChunk(
            session_id="exist123",
            chunk_text="Q: Q?\nA: A.",
            created_at=datetime(2025, 1, 1),
            metadata={"transcript_path": "/tmp/t.jsonl", "chunk_index": 0},
        )
    ]
    insert_chunks(chunks, [[0.1] * 768])
    assert session_exists("exist123")


def test_created_at_is_explicit(temp_db):
    explicit_time = datetime(2025, 3, 15, 10, 30, 45)
    chunks = [
        MemoryChunk(
            session_id="timecheck",
            chunk_text="Test",
            created_at=explicit_time,
            metadata={},
        )
    ]
    insert_chunks(chunks, [[0.0] * 768])
    import sqlite3

    conn = sqlite3.connect(str(temp_db))
    cursor = conn.cursor()
    cursor.execute("SELECT created_at FROM memories WHERE session_id = 'timecheck'")
    result = cursor.fetchone()[0]
    assert "2025-03-15" in result
    conn.close()


def test_get_session_import_info(temp_db):
    chunks = [
        MemoryChunk(
            session_id="meta123",
            chunk_text="Q: Q?\nA: A.",
            created_at=datetime(2025, 1, 1),
            metadata={
                "transcript_path": "/tmp/t.jsonl",
                "chunk_index": 0,
                "transcript_mtime": 123.4,
            },
        )
    ]
    insert_chunks(chunks, [[0.1] * 768])
    info = get_session_import_info("meta123")
    assert info is not None
    assert info.chunk_count == 1
    assert info.transcript_mtime == 123.4


def test_delete_session(temp_db):
    chunks = [
        MemoryChunk(
            session_id="delete123",
            chunk_text="Q: Q?\nA: A.",
            created_at=datetime(2025, 1, 1),
            metadata={"transcript_path": "/tmp/t.jsonl", "chunk_index": 0},
        )
    ]
    insert_chunks(chunks, [[0.1] * 768])
    delete_session("delete123")
    assert not session_exists("delete123")


def test_import_state_round_trip(temp_db):
    assert get_import_state("watch-codex:test") is None
    set_import_state("watch-codex:test", "123.4")
    state = get_import_state("watch-codex:test")
    assert state is not None
    assert state.state_key == "watch-codex:test"
    assert state.state_value == "123.4"

    set_import_state("watch-codex:test", "456.7")
    updated_state = get_import_state("watch-codex:test")
    assert updated_state is not None
    assert updated_state.state_value == "456.7"


def test_normalize_fts_query_quotes_terms():
    assert normalize_fts_query("watch-all watcher") == '"watch-all" OR "watcher"'
    assert normalize_fts_query('foo "bar"') == '"foo" OR """bar"""'
    assert normalize_fts_query("   ") == ""
