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


def _iter_codex_session_files(sessions_dir: Path) -> list[Path]:
    return sorted(p for p in sessions_dir.rglob("*.jsonl") if p.is_file())


def _iter_opencode_session_ids(db_path: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id
        FROM session
        WHERE time_archived IS NULL
        ORDER BY time_updated ASC
        """
    )
    session_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return session_ids


def _import_codex_sessions(sessions_dir: Path, limit: int | None = None) -> tuple[int, int]:
    storage.init_db()
    files = _iter_codex_session_files(sessions_dir)
    if limit is not None:
        files = files[-limit:]
    imported = 0
    skipped = 0
    for file_path in files:
        chunks, _ = chunker.parse_codex_transcript(file_path)
        if _save_chunks(chunks):
            imported += 1
        else:
            skipped += 1
    return imported, skipped


def _import_opencode_sessions(db_path: Path, limit: int | None = None) -> tuple[int, int]:
    storage.init_db()
    session_ids = _iter_opencode_session_ids(db_path)
    if limit is not None:
        session_ids = session_ids[-limit:]
    imported = 0
    skipped = 0
    for session_id in session_ids:
        chunks, _ = chunker.parse_opencode_session(db_path, session_id)
        if _save_chunks(chunks):
            imported += 1
        else:
            skipped += 1
    return imported, skipped


def cmd_import_codex(args: argparse.Namespace) -> None:
    sessions_dir = Path(args.dir).expanduser()
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Codex sessions directory not found: {sessions_dir}")
    imported, skipped = _import_codex_sessions(sessions_dir, limit=args.limit)
    print(f"Imported Codex sessions: {imported}")
    print(f"Skipped Codex sessions: {skipped}")


def cmd_watch_codex(args: argparse.Namespace) -> None:
    sessions_dir = Path(args.dir).expanduser()
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Codex sessions directory not found: {sessions_dir}")
    logger.info(f"Watching Codex sessions under {sessions_dir}")
    while True:
        imported, skipped = _import_codex_sessions(sessions_dir, limit=args.limit)
        logger.info(f"Codex import cycle complete. imported={imported} skipped={skipped}")
        time.sleep(args.interval)


def cmd_import_opencode(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    imported, skipped = _import_opencode_sessions(db_path, limit=args.limit)
    print(f"Imported OpenCode sessions: {imported}")
    print(f"Skipped OpenCode sessions: {skipped}")


def cmd_watch_opencode(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")
    logger.info(f"Watching OpenCode database at {db_path}")
    while True:
        imported, skipped = _import_opencode_sessions(db_path, limit=args.limit)
        logger.info(
            f"OpenCode import cycle complete. imported={imported} skipped={skipped}"
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
    args = parser.parse_args()
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
