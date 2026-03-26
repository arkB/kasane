import argparse
import logging
import sys

from . import chunker
from . import embedder
from . import search
from . import storage

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def cmd_warmup(args: argparse.Namespace) -> None:
    logger.info("Starting warmup...")
    embedder.warmup()
    storage.init_db()
    logger.info("Warmup complete.")


def cmd_save(args: argparse.Namespace) -> None:
    transcript_path = args.transcript
    logger.info(f"Saving transcript: {transcript_path}")
    storage.init_db()
    chunks, _ = chunker.parse_transcript(transcript_path)
    if not chunks:
        logger.warning("No chunks to save.")
        return
    session_id = chunks[0].session_id
    if storage.session_exists(session_id):
        logger.info(f"Session {session_id} already exists. Skipping.")
        return
    embeddings = embedder.encode([c.chunk_text for c in chunks], prefix="passage")
    if isinstance(embeddings[0], float):
        embeddings = [embeddings]
    storage.insert_chunks(chunks, embeddings)
    logger.info(f"Saved {len(chunks)} chunks for session {session_id}.")


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
