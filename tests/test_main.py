import sqlite3
from pathlib import Path

from kasane import main


def test_iter_codex_session_files_filters_by_mtime(tmp_path):
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "nested" / "newer.jsonl"
    newer.parent.mkdir()
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")

    older_mtime = 100.0
    newer_mtime = 200.0
    older.touch()
    newer.touch()
    import os

    os.utime(older, (older_mtime, older_mtime))
    os.utime(newer, (newer_mtime, newer_mtime))

    all_files = main._iter_codex_session_files(tmp_path)
    assert all_files == [older, newer]

    filtered_files = main._iter_codex_session_files(tmp_path, min_mtime=150.0)
    assert filtered_files == [newer]


def test_iter_opencode_sessions_filters_by_update_time(tmp_path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        );
        """
    )
    cursor.executemany(
        "INSERT INTO session (id, time_updated, time_archived) VALUES (?, ?, ?)",
        [
            ("old", 1000, None),
            ("archived", 2000, 3000),
            ("new", 4000, None),
        ],
    )
    conn.commit()
    conn.close()

    all_sessions = main._iter_opencode_sessions(db_path)
    assert all_sessions == [("old", 1000), ("new", 4000)]

    filtered_sessions = main._iter_opencode_sessions(db_path, min_updated_ms=2500)
    assert filtered_sessions == [("new", 4000)]


def test_load_watch_watermark_uses_lookback(monkeypatch):
    class DummyState:
        state_value = "600"

    monkeypatch.setattr(main.storage, "get_import_state", lambda _key: DummyState())
    assert main._load_watch_watermark("watch-codex:test") == 300.0


def test_get_state_key_resolves_path(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    expected = f"watch-codex:{sessions_dir.resolve()}"
    assert main._get_state_key("watch-codex", Path(str(sessions_dir))) == expected


def test_get_latest_codex_mtime(tmp_path):
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "newer.jsonl"
    older.write_text("{}\n", encoding="utf-8")
    newer.write_text("{}\n", encoding="utf-8")

    import os

    os.utime(older, (100.0, 100.0))
    os.utime(newer, (200.0, 200.0))

    assert main._get_latest_codex_mtime(tmp_path) == 200.0


def test_get_latest_opencode_updated_ms(tmp_path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        );
        """
    )
    cursor.executemany(
        "INSERT INTO session (id, time_updated, time_archived) VALUES (?, ?, ?)",
        [
            ("a", 1000, None),
            ("b", 4000, None),
            ("c", 2000, 3000),
        ],
    )
    conn.commit()
    conn.close()

    assert main._get_latest_opencode_updated_ms(db_path) == 4000


def test_is_settled_uses_settle_window():
    assert main._is_settled(100.0, now=221.0)
    assert not main._is_settled(102.0, now=221.0)


def test_is_session_current(monkeypatch):
    monkeypatch.setattr(
        main.storage,
        "get_session_import_info",
        lambda _session_id: type(
            "Info", (), {"transcript_mtime": 200.0, "chunk_count": 3}
        )(),
    )
    assert main._is_session_current("session-1", 200.0)
    assert main._is_session_current("session-1", 150.0)
    assert not main._is_session_current("session-1", 250.0)
