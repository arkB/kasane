from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import logging
import sqlite3
from typing import Optional

import sqlite_vec

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent.parent / "data" / "memory.db"


@dataclass
class MemoryChunk:
    session_id: str
    chunk_text: str
    created_at: datetime
    metadata: dict


@dataclass
class MemoryResult:
    id: int
    chunk_text: str
    score: float
    created_at: datetime
    session_id: str


@dataclass
class SessionImportInfo:
    chunk_count: int
    transcript_mtime: float | None


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            metadata JSON
        );

        CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories(session_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            chunk_text,
            content='memories',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
            id INTEGER PRIMARY KEY,
            embedding FLOAT[768]
        );
    """)
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='memories_ai'"
    )
    if not cursor.fetchone():
        cursor.execute("""
            CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
            END;
        """)
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='memories_ad'"
    )
    if not cursor.fetchone():
        cursor.execute("""
            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, chunk_text)
                VALUES('delete', old.id, old.chunk_text);
            END;
        """)
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='memories_au'"
    )
    if not cursor.fetchone():
        cursor.execute("""
            CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, chunk_text)
                VALUES('delete', old.id, old.chunk_text);
                INSERT INTO memories_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
            END;
        """)
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


def session_exists(session_id: str) -> bool:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM memories WHERE session_id = ? LIMIT 1", (session_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def get_session_import_info(session_id: str) -> SessionImportInfo | None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT metadata FROM memories WHERE session_id = ? ORDER BY id LIMIT 1",
        (session_id,),
    )
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None
    cursor.execute("SELECT COUNT(*) FROM memories WHERE session_id = ?", (session_id,))
    chunk_count = cursor.fetchone()[0]
    transcript_mtime = None
    metadata_raw = row[0]
    if metadata_raw:
        metadata = json.loads(metadata_raw)
        transcript_mtime_raw = metadata.get("transcript_mtime")
        if transcript_mtime_raw is not None:
            transcript_mtime = float(transcript_mtime_raw)
    conn.close()
    return SessionImportInfo(
        chunk_count=chunk_count,
        transcript_mtime=transcript_mtime,
    )


def delete_session(session_id: str) -> None:
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM memories WHERE session_id = ?", (session_id,))
        memory_ids = [row[0] for row in cursor.fetchall()]
        if memory_ids:
            placeholders = ",".join("?" * len(memory_ids))
            cursor.execute(
                f"DELETE FROM memories_vec WHERE id IN ({placeholders})",
                memory_ids,
            )
        cursor.execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_chunks(chunks: list[MemoryChunk], embeddings: list[list[float]]) -> None:
    if not chunks:
        return
    conn = _get_connection()
    cursor = conn.cursor()
    try:
        for chunk, embedding in zip(chunks, embeddings):
            cursor.execute(
                "INSERT INTO memories (session_id, chunk_text, created_at, metadata) VALUES (?, ?, ?, ?)",
                (
                    chunk.session_id,
                    chunk.chunk_text,
                    chunk.created_at.isoformat(),
                    json.dumps(chunk.metadata),
                ),
            )
            row_id = cursor.lastrowid
            embedding_bytes = _embedding_to_bytes(embedding)
            cursor.execute(
                "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
                (row_id, embedding_bytes),
            )
        conn.commit()
        logger.info(f"Inserted {len(chunks)} chunks for session {chunks[0].session_id}")
    except Exception as e:
        conn.rollback()
        logger.warning(f"Failed to insert chunks: {e}")
        raise
    finally:
        conn.close()


def _embedding_to_bytes(embedding: list[float]) -> bytes:
    import struct

    return struct.pack(f"<{len(embedding)}f", *embedding)


def _bytes_to_embedding(data: bytes) -> list[float]:
    import struct

    return list(struct.unpack(f"<{len(data) // 4}f", data))


def fts_search(query: str, limit: int = 50) -> list[tuple[int, int]]:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    )
    results = [(row[0], rank + 1) for rank, row in enumerate(cursor.fetchall())]
    conn.close()
    return results


def vec_search(embedding: list[float], limit: int = 50) -> list[tuple[int, float]]:
    conn = _get_connection()
    cursor = conn.cursor()
    embedding_bytes = _embedding_to_bytes(embedding)
    cursor.execute(
        "SELECT id, distance FROM memories_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (embedding_bytes, limit),
    )
    results = [(row[0], row[1]) for row in cursor.fetchall()]
    conn.close()
    return results


def get_memories_by_ids(ids: list[int]) -> dict[int, MemoryResult]:
    if not ids:
        return {}
    conn = _get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(ids))
    cursor.execute(
        f"SELECT id, session_id, chunk_text, created_at FROM memories WHERE id IN ({placeholders})",
        ids,
    )
    results = {}
    for row in cursor.fetchall():
        results[row[0]] = MemoryResult(
            id=row[0],
            session_id=row[1],
            chunk_text=row[2],
            created_at=datetime.fromisoformat(row[3]),
            score=0.0,
        )
    conn.close()
    return results


def get_stats() -> dict:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM memories")
    total_memories = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT session_id) FROM memories")
    total_sessions = cursor.fetchone()[0]
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    conn.close()
    return {
        "total_memories": total_memories,
        "total_sessions": total_sessions,
        "db_size_bytes": db_size,
    }


def optimize_db() -> None:
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("VACUUM")
    cursor.execute("ANALYZE")
    conn.commit()
    conn.close()
    logger.info("Database optimized")
