import argparse
import logging
import sys
import time
from pathlib import Path

from . import chunker
from . import embedder
from . import search
from . import storage

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_OPENCODE_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
WATCH_LOOKBACK_SECONDS = 300
WATCH_SETTLE_SECONDS = 120


def _normalize_cli_argv(argv: list[str]) -> list[str]:
    if len(argv) < 4 or argv[1] != "search":
        return argv
    normalized = list(argv)
    for i in range(2, len(normalized) - 1):
        if normalized[i] != "--query":
            continue
        value = normalized[i + 1]
        if value.startswith("-"):
            normalized[i] = f"--query={value}"
            del normalized[i + 1]
        break
    return normalized


def cmd_warmup(args: argparse.Namespace) -> None:
    logger.info("Starting warmup...")
    embedder.warmup()
    storage.init_db()
    logger.info("Warmup complete.")


def _save_chunks(chunks: list[storage.MemoryChunk]) -> bool:
    if not chunks:
        logger.warning("No chunks to save.")
        return False
    session_id = chunks[0].session_id
    incoming_mtime_raw = chunks[0].metadata.get("transcript_mtime")
    incoming_mtime = (
        float(incoming_mtime_raw) if incoming_mtime_raw is not None else None
    )
    existing = storage.get_session_import_info(session_id)
    if existing is not None:
        is_newer = (
            incoming_mtime is not None
            and (
                existing.transcript_mtime is None
                or incoming_mtime > existing.transcript_mtime
            )
        )
        has_more_chunks = len(chunks) > existing.chunk_count
        if not is_newer and not has_more_chunks:
            logger.info(f"Session {session_id} already exists. Skipping.")
            return False
        logger.info(f"Replacing stored session {session_id} with a newer transcript.")
        storage.delete_session(session_id)
    embeddings = embedder.encode([c.chunk_text for c in chunks], prefix="passage")
    if isinstance(embeddings[0], float):
        embeddings = [embeddings]
    storage.insert_chunks(chunks, embeddings)
    logger.info(f"Saved {len(chunks)} chunks for session {session_id}.")
    return True


def _is_session_current(session_id: str, incoming_mtime: float | None) -> bool:
    if incoming_mtime is None:
        return False
    existing = storage.get_session_import_info(session_id)
    if existing is None or existing.transcript_mtime is None:
        return False
    return existing.transcript_mtime >= incoming_mtime


def cmd_save(args: argparse.Namespace) -> None:
    transcript_path = args.transcript
    logger.info(f"Saving transcript: {transcript_path}")
    storage.init_db()
    chunks, _ = chunker.parse_transcript(transcript_path)
    _save_chunks(chunks)


def cmd_search(args: argparse.Namespace) -> None:
    query = args.query
    top_k = args.top_k if hasattr(args, "top_k") else 5
    storage.init_db()
    results = search.hybrid_search(query, top_k=top_k)
    if not results:
        print(f"No memories found for: {query}")
        return
    for i, result in enumerate(results, 1):
        created_at_str = result.created_at.strftime("%Y-%m-%d")
        print(
            f"[{i}/{len(results)}] score={result.score:.4f} | {created_at_str} | session={result.session_id}"
        )
        print(result.chunk_text)
        if i < len(results):
            print("---")


def cmd_stats(args: argparse.Namespace) -> None:
    storage.init_db()
    stats = storage.get_stats()
    print(f"Total memories: {stats['total_memories']}")
    print(f"Total sessions: {stats['total_sessions']}")
    db_size_mb = stats["db_size_bytes"] / (1024 * 1024)
    print(f"Database size: {db_size_mb:.2f} MB")


def cmd_optimize(args: argparse.Namespace) -> None:
    storage.init_db()
    storage.optimize_db()
    logger.info("Database optimization complete.")


def _iter_codex_session_files(
    sessions_dir: Path, min_mtime: float | None = None
) -> list[Path]:
    files = []
    for path in sessions_dir.rglob("*.jsonl"):
        if not path.is_file():
            continue
        if min_mtime is not None and path.stat().st_mtime < min_mtime:
            continue
        files.append(path)
    return sorted(files, key=lambda path: (path.stat().st_mtime, str(path)))


def _iter_opencode_sessions(
    db_path: Path, min_updated_ms: int | None = None
) -> list[tuple[str, int]]:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    query = """
        SELECT id, time_updated
        FROM session
        WHERE time_archived IS NULL
    """
    params: tuple[object, ...] = ()
    if min_updated_ms is not None:
        query += " AND time_updated >= ?"
        params = (min_updated_ms,)
    query += " ORDER BY time_updated ASC"
    cursor.execute(query, params)
    sessions = [(str(row[0]), int(row[1])) for row in cursor.fetchall()]
    conn.close()
    return sessions


def _get_state_key(prefix: str, source_path: Path) -> str:
    return f"{prefix}:{source_path.expanduser().resolve()}"


def _load_watch_watermark(state_key: str) -> float | None:
    state = storage.get_import_state(state_key)
    if state is None:
        return None
    return max(0.0, float(state.state_value) - WATCH_LOOKBACK_SECONDS)


def _get_latest_codex_mtime(sessions_dir: Path) -> float | None:
    files = _iter_codex_session_files(sessions_dir)
    if not files:
        return None
    return max(path.stat().st_mtime for path in files)


def _get_latest_opencode_updated_ms(db_path: Path) -> int | None:
    sessions = _iter_opencode_sessions(db_path)
    if not sessions:
        return None
    return max(updated_ms for _, updated_ms in sessions)


def _is_settled(timestamp_seconds: float, now: float | None = None) -> bool:
    if now is None:
        now = time.time()
    return timestamp_seconds <= now - WATCH_SETTLE_SECONDS


def _seed_codex_watch_state(sessions_dir: Path) -> str:
    state_key = _get_state_key("watch-codex", sessions_dir)
    if storage.get_import_state(state_key) is None:
        latest_mtime = _get_latest_codex_mtime(sessions_dir)
        if latest_mtime is not None:
            storage.set_import_state(state_key, str(latest_mtime))
            logger.info(
                "Seeded Codex watcher at current session boundary. "
                "Run import-codex separately to backfill older history."
            )
    return state_key


def _seed_opencode_watch_state(db_path: Path) -> str:
    state_key = _get_state_key("watch-opencode", db_path)
    if storage.get_import_state(state_key) is None:
        latest_updated_ms = _get_latest_opencode_updated_ms(db_path)
        if latest_updated_ms is not None:
            storage.set_import_state(state_key, str(latest_updated_ms / 1000))
            logger.info(
                "Seeded OpenCode watcher at current session boundary. "
                "Run import-opencode separately to backfill older history."
            )
    return state_key


def _import_codex_sessions(
    sessions_dir: Path,
    limit: int | None = None,
    min_mtime: float | None = None,
) -> tuple[int, int, float | None]:
    storage.init_db()
    files = _iter_codex_session_files(sessions_dir, min_mtime=min_mtime)
    if limit is not None:
        files = files[-limit:]
    imported = 0
    skipped = 0
    max_mtime = None
    now = time.time()
    for file_path in files:
        file_mtime = file_path.stat().st_mtime
        if max_mtime is None or file_mtime > max_mtime:
            max_mtime = file_mtime
        if not _is_settled(file_mtime, now=now):
            skipped += 1
            continue
        session_id, _ = chunker._extract_codex_session_info(file_path)
        if _is_session_current(session_id, file_mtime):
            skipped += 1
            continue
        chunks, _ = chunker.parse_codex_transcript(file_path)
        if _save_chunks(chunks):
            imported += 1
        else:
            skipped += 1
    return imported, skipped, max_mtime


def _import_opencode_sessions(
    db_path: Path,
    limit: int | None = None,
    min_updated_ms: int | None = None,
) -> tuple[int, int, int | None]:
    storage.init_db()
    sessions = _iter_opencode_sessions(db_path, min_updated_ms=min_updated_ms)
    if limit is not None:
        sessions = sessions[-limit:]
    imported = 0
    skipped = 0
    max_updated_ms = None
    now = time.time()
    for session_id, updated_ms in sessions:
        if max_updated_ms is None or updated_ms > max_updated_ms:
            max_updated_ms = updated_ms
        if not _is_settled(updated_ms / 1000, now=now):
            skipped += 1
            continue
        if _is_session_current(session_id, updated_ms / 1000):
            skipped += 1
            continue
        chunks, _ = chunker.parse_opencode_session(db_path, session_id)
        if _save_chunks(chunks):
            imported += 1
        else:
            skipped += 1
    return imported, skipped, max_updated_ms


def _run_codex_watch_cycle(sessions_dir: Path, limit: int | None, state_key: str) -> tuple[int, int]:
    min_mtime = _load_watch_watermark(state_key)
    imported, skipped, max_mtime = _import_codex_sessions(
        sessions_dir, limit=limit, min_mtime=min_mtime
    )
    if max_mtime is not None:
        storage.set_import_state(state_key, str(max_mtime))
    logger.info(f"Codex import cycle complete. imported={imported} skipped={skipped}")
    return imported, skipped


def _run_opencode_watch_cycle(db_path: Path, limit: int | None, state_key: str) -> tuple[int, int]:
    min_updated_ms = _load_watch_watermark(state_key)
    imported, skipped, max_updated_ms = _import_opencode_sessions(
        db_path,
        limit=limit,
        min_updated_ms=None if min_updated_ms is None else int(min_updated_ms * 1000),
    )
    if max_updated_ms is not None:
        storage.set_import_state(state_key, str(max_updated_ms / 1000))
    logger.info(
        f"OpenCode import cycle complete. imported={imported} skipped={skipped}"
    )
    return imported, skipped


def cmd_import_codex(args: argparse.Namespace) -> None:
    sessions_dir = Path(args.dir).expanduser()
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Codex sessions directory not found: {sessions_dir}")
    imported, skipped, _ = _import_codex_sessions(sessions_dir, limit=args.limit)
    print(f"Imported Codex sessions: {imported}")
    print(f"Skipped Codex sessions: {skipped}")


def cmd_watch_codex(args: argparse.Namespace) -> None:
    sessions_dir = Path(args.dir).expanduser()
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Codex sessions directory not found: {sessions_dir}")
    storage.init_db()
    state_key = _seed_codex_watch_state(sessions_dir)
    logger.info(f"Watching Codex sessions under {sessions_dir}")
    while True:
        _run_codex_watch_cycle(sessions_dir, limit=args.limit, state_key=state_key)
        time.sleep(args.interval)


def cmd_import_opencode(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    imported, skipped, _ = _import_opencode_sessions(db_path, limit=args.limit)
    print(f"Imported OpenCode sessions: {imported}")
    print(f"Skipped OpenCode sessions: {skipped}")


def cmd_watch_opencode(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    storage.init_db()
    state_key = _seed_opencode_watch_state(db_path)
    logger.info(f"Watching OpenCode database at {db_path}")
    while True:
        _run_opencode_watch_cycle(db_path, limit=args.limit, state_key=state_key)
        time.sleep(args.interval)


def cmd_watch_all(args: argparse.Namespace) -> None:
    sessions_dir = Path(args.codex_dir).expanduser()
    db_path = Path(args.opencode_db).expanduser()
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Codex sessions directory not found: {sessions_dir}")
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    storage.init_db()
    codex_state_key = _seed_codex_watch_state(sessions_dir)
    opencode_state_key = _seed_opencode_watch_state(db_path)
    logger.info(
        f"Watching Codex sessions under {sessions_dir} and OpenCode database at {db_path}"
    )
    while True:
        _run_codex_watch_cycle(
            sessions_dir, limit=args.codex_limit, state_key=codex_state_key
        )
        _run_opencode_watch_cycle(
            db_path, limit=args.opencode_limit, state_key=opencode_state_key
        )
        time.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="kasane - Claude Code 長期記憶システム"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    warmup_parser = subparsers.add_parser(
        "warmup", help="Download and cache the embedding model"
    )
    warmup_parser.set_defaults(func=cmd_warmup)
    save_parser = subparsers.add_parser("save", help="Save a transcript to memory")
    save_parser.add_argument(
        "--transcript", required=True, help="Path to transcript JSONL file"
    )
    save_parser.set_defaults(func=cmd_save)
    search_parser = subparsers.add_parser("search", help="Search memories")
    search_parser.add_argument("--query", required=True, help="Search query")
    search_parser.add_argument(
        "--top-k", type=int, default=5, help="Number of results (default: 5)"
    )
    search_parser.set_defaults(func=cmd_search)
    stats_parser = subparsers.add_parser("stats", help="Show memory statistics")
    stats_parser.set_defaults(func=cmd_stats)
    optimize_parser = subparsers.add_parser("optimize", help="Optimize the database")
    optimize_parser.set_defaults(func=cmd_optimize)
    import_codex_parser = subparsers.add_parser(
        "import-codex", help="Import completed Codex sessions into memory"
    )
    import_codex_parser.add_argument(
        "--dir",
        default=str(DEFAULT_CODEX_SESSIONS_DIR),
        help=f"Codex sessions directory (default: {DEFAULT_CODEX_SESSIONS_DIR})",
    )
    import_codex_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only inspect the most recent N session files",
    )
    import_codex_parser.set_defaults(func=cmd_import_codex)
    watch_codex_parser = subparsers.add_parser(
        "watch-codex", help="Continuously import new Codex sessions"
    )
    watch_codex_parser.add_argument(
        "--dir",
        default=str(DEFAULT_CODEX_SESSIONS_DIR),
        help=f"Codex sessions directory (default: {DEFAULT_CODEX_SESSIONS_DIR})",
    )
    watch_codex_parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)",
    )
    watch_codex_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Only inspect the most recent N session files per polling cycle",
    )
    watch_codex_parser.set_defaults(func=cmd_watch_codex)
    import_opencode_parser = subparsers.add_parser(
        "import-opencode", help="Import completed OpenCode sessions into memory"
    )
    import_opencode_parser.add_argument(
        "--db",
        default=str(DEFAULT_OPENCODE_DB_PATH),
        help=f"OpenCode database path (default: {DEFAULT_OPENCODE_DB_PATH})",
    )
    import_opencode_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only inspect the most recent N sessions",
    )
    import_opencode_parser.set_defaults(func=cmd_import_opencode)
    watch_opencode_parser = subparsers.add_parser(
        "watch-opencode", help="Continuously import new OpenCode sessions"
    )
    watch_opencode_parser.add_argument(
        "--db",
        default=str(DEFAULT_OPENCODE_DB_PATH),
        help=f"OpenCode database path (default: {DEFAULT_OPENCODE_DB_PATH})",
    )
    watch_opencode_parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)",
    )
    watch_opencode_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Only inspect the most recent N sessions per polling cycle",
    )
    watch_opencode_parser.set_defaults(func=cmd_watch_opencode)
    watch_all_parser = subparsers.add_parser(
        "watch-all", help="Continuously import new Codex and OpenCode sessions"
    )
    watch_all_parser.add_argument(
        "--codex-dir",
        default=str(DEFAULT_CODEX_SESSIONS_DIR),
        help=f"Codex sessions directory (default: {DEFAULT_CODEX_SESSIONS_DIR})",
    )
    watch_all_parser.add_argument(
        "--opencode-db",
        default=str(DEFAULT_OPENCODE_DB_PATH),
        help=f"OpenCode database path (default: {DEFAULT_OPENCODE_DB_PATH})",
    )
    watch_all_parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Polling interval in seconds (default: 30)",
    )
    watch_all_parser.add_argument(
        "--codex-limit",
        type=int,
        default=100,
        help="Only inspect the most recent N Codex session files per polling cycle",
    )
    watch_all_parser.add_argument(
        "--opencode-limit",
        type=int,
        default=100,
        help="Only inspect the most recent N OpenCode sessions per polling cycle",
    )
    watch_all_parser.set_defaults(func=cmd_watch_all)
    args = parser.parse_args(_normalize_cli_argv(sys.argv)[1:])
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except Exception as e:
        logger.warning(f"Error during {args.command}: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
