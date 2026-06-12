import sqlite3
import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_memory_quality.py"
SPEC = importlib.util.spec_from_file_location("audit_memory_quality", SCRIPT_PATH)
audit_memory_quality = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit_memory_quality)


def create_memory_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            metadata JSON
        )
        """
    )
    conn.execute(
        "INSERT INTO memories (session_id, chunk_text, created_at, metadata) VALUES (?, ?, ?, ?)",
        (
            "claude-session",
            "Q: {'role': 'user'}\nA: thinking signature tool_use",
            "2026-06-12T00:00:00",
            '{"transcript_path": "/home/user/.claude/projects/example.jsonl", "chunk_index": 0}',
        ),
    )
    conn.execute(
        "INSERT INTO memories (session_id, chunk_text, created_at, metadata) VALUES (?, ?, ?, ?)",
        (
            "clean-session",
            "Q: normal request\nA: normal response",
            "2026-06-12T00:00:01",
            '{"transcript_path": "opencode-db:/tmp/opencode.db#clean", "chunk_index": 0}',
        ),
    )
    conn.commit()
    conn.close()


def create_db_with_rows(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            metadata JSON
        )
        """
    )
    for session_id, chunk_text, metadata in rows:
        conn.execute(
            "INSERT INTO memories (session_id, chunk_text, created_at, metadata) VALUES (?, ?, ?, ?)",
            (session_id, chunk_text, "2026-06-12T00:00:00", metadata),
        )
    conn.commit()
    conn.close()


def test_audit_counts_markers_source_kind_and_classification(tmp_path):
    db_path = tmp_path / "memory.db"
    create_memory_db(db_path)

    rows = audit_memory_quality.load_rows(db_path)
    marker_counts, source_counts, classification_counts, hits = (
        audit_memory_quality.audit(rows)
    )

    assert len(rows) == 2
    assert len(hits) == 1
    assert hits[0].classification == "literal_discussion"
    assert marker_counts["{'role':"] == 1
    assert marker_counts["thinking"] == 1
    assert marker_counts["signature"] == 1
    assert marker_counts["tool_use"] == 1
    assert source_counts["claude"] == 1
    assert source_counts["opencode"] == 0
    assert classification_counts["literal_discussion"] == 1


def test_auto_meta_classification(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: <environment_context>\n  <cwd>/repo</cwd>\n</environment_context>\nA: ",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    _, _, classification_counts, hits = audit_memory_quality.audit(
        audit_memory_quality.load_rows(db_path)
    )

    assert len(hits) == 1
    assert hits[0].classification == "auto_meta"
    assert classification_counts["auto_meta"] == 1


def test_internal_context_classification(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: <codex_internal_context source=\"goal\">\nsecret\n</codex_internal_context>\nA: ",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    _, _, classification_counts, hits = audit_memory_quality.audit(
        audit_memory_quality.load_rows(db_path)
    )

    assert len(hits) == 1
    assert hits[0].classification == "internal_context"
    assert classification_counts["internal_context"] == 1


def test_literal_discussion_classification(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: Codex parser の `<environment_context>` 除外を実装してください。\nA: done",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    _, _, classification_counts, hits = audit_memory_quality.audit(
        audit_memory_quality.load_rows(db_path)
    )

    assert len(hits) == 1
    assert hits[0].classification == "literal_discussion"
    assert classification_counts["literal_discussion"] == 1


def test_fail_on_hit_exit_code_for_auto_meta(tmp_path, capsys):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: <environment_context>\n  <cwd>/repo</cwd>\n</environment_context>\nA: ",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    exit_code = audit_memory_quality.main(
        ["--db-path", str(db_path), "--limit", "1", "--fail-on-hit"]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert f"DB path: {db_path}" in output
    assert "Unique hit rows: 1" in output
    assert "Failure hit rows: 1" in output


def test_fail_on_hit_exit_code_for_internal_context(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: <codex_internal_context source=\"goal\">secret</codex_internal_context>\nA: ",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    assert audit_memory_quality.main(
        ["--db-path", str(db_path), "--fail-on-hit"]
    ) == 1


def test_fail_on_hit_ignores_literal_discussion_by_default(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: Codex parser の `<environment_context>` 除外を実装してください。\nA: done",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    assert audit_memory_quality.main(
        ["--db-path", str(db_path), "--fail-on-hit"]
    ) == 0


def test_strict_literal_fails_literal_discussion(tmp_path):
    db_path = tmp_path / "memory.db"
    create_db_with_rows(
        db_path,
        [
            (
                "codex-session",
                "Q: Codex parser の `<environment_context>` 除外を実装してください。\nA: done",
                '{"transcript_path": "/home/user/.codex/sessions/example.jsonl"}',
            )
        ],
    )

    assert audit_memory_quality.main(
        ["--db-path", str(db_path), "--fail-on-hit", "--strict-literal"]
    ) == 1


def test_clean_db_returns_success_with_fail_on_hit(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            metadata JSON
        )
        """
    )
    conn.execute(
        "INSERT INTO memories (session_id, chunk_text, created_at, metadata) VALUES (?, ?, ?, ?)",
        (
            "clean-session",
            "Q: normal request\nA: normal response",
            "2026-06-12T00:00:00",
            '{"transcript_path": "/home/user/.codex/sessions/example.jsonl", "chunk_index": 0}',
        ),
    )
    conn.commit()
    conn.close()

    assert audit_memory_quality.main(
        ["--db-path", str(db_path), "--fail-on-hit"]
    ) == 0
