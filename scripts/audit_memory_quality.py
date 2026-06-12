#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


AUTO_META_CLASSIFICATION = "auto_meta"
INTERNAL_CONTEXT_CLASSIFICATION = "internal_context"
LITERAL_DISCUSSION_CLASSIFICATION = "literal_discussion"
FAIL_CLASSIFICATIONS = {
    AUTO_META_CLASSIFICATION,
    INTERNAL_CONTEXT_CLASSIFICATION,
}

MARKERS = [
    "{'role':",
    '"role":',
    "thinking",
    "signature",
    "tool_use",
    "tool_result",
    "<environment_context>",
    "<codex_internal_context",
    "<cwd>",
    "approval_policy",
    "sandbox_mode",
    "network_access",
    "file-history-snapshot",
    "permission-mode",
]


@dataclass
class MemoryRow:
    row_id: int
    session_id: str
    created_at: str
    chunk_text: str
    metadata: dict


@dataclass
class Hit:
    row: MemoryRow
    markers: list[str]
    classification: str


def default_db_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root.parent / "data" / "memory.db"


def resolve_db_path(cli_db_path: str | None) -> Path:
    if cli_db_path:
        return Path(cli_db_path).expanduser()
    env_db_path = os.environ.get("KASANE_DB_PATH")
    if env_db_path:
        return Path(env_db_path).expanduser()
    return default_db_path()


def source_kind(transcript_path: str) -> str:
    if ".claude" in transcript_path:
        return "claude"
    if ".codex" in transcript_path:
        return "codex"
    if transcript_path.startswith("opencode-db:") or "opencode" in transcript_path:
        return "opencode"
    return "unknown"


def load_metadata(raw_metadata: str | None) -> dict:
    if not raw_metadata:
        return {}
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def transcript_path(metadata: dict) -> str:
    value = metadata.get("transcript_path") or metadata.get("source") or ""
    return str(value)


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_rows(db_path: Path) -> list[MemoryRow]:
    conn = connect_read_only(db_path)
    try:
        cursor = conn.execute(
            "SELECT id, session_id, created_at, chunk_text, metadata FROM memories ORDER BY id"
        )
        return [
            MemoryRow(
                row_id=int(row[0]),
                session_id=str(row[1]),
                created_at=str(row[2]),
                chunk_text=str(row[3]),
                metadata=load_metadata(row[4]),
            )
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def total_sessions(rows: list[MemoryRow]) -> int:
    return len({row.session_id for row in rows})


def matching_markers(chunk_text: str) -> list[str]:
    return [marker for marker in MARKERS if marker in chunk_text]


def classify_hit(chunk_text: str) -> str:
    question_text = chunk_text.strip()
    if question_text.startswith("Q:"):
        question_text = question_text[2:].lstrip()

    if question_text.startswith("<codex_internal_context"):
        return INTERNAL_CONTEXT_CLASSIFICATION
    if "<codex_internal_context" in chunk_text:
        return INTERNAL_CONTEXT_CLASSIFICATION
    if question_text.startswith("<environment_context>"):
        return AUTO_META_CLASSIFICATION
    if "<cwd>" in chunk_text and "<environment_context>" in chunk_text:
        return AUTO_META_CLASSIFICATION
    return LITERAL_DISCUSSION_CLASSIFICATION


def failure_classifications(strict_literal: bool) -> set[str]:
    classifications = set(FAIL_CLASSIFICATIONS)
    if strict_literal:
        classifications.add(LITERAL_DISCUSSION_CLASSIFICATION)
    return classifications


def audit(rows: list[MemoryRow]) -> tuple[Counter[str], Counter[str], Counter[str], list[Hit]]:
    marker_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    hits: list[Hit] = []
    for row in rows:
        row_markers = matching_markers(row.chunk_text)
        if not row_markers:
            continue
        classification = classify_hit(row.chunk_text)
        hits.append(Hit(row=row, markers=row_markers, classification=classification))
        for marker in row_markers:
            marker_counts[marker] += 1
        source_counts[source_kind(transcript_path(row.metadata))] += 1
        classification_counts[classification] += 1
    return marker_counts, source_counts, classification_counts, hits


def sample_text(text: str, max_chars: int = 200) -> str:
    return text[:max_chars].replace("\n", "\\n")


def print_report(db_path: Path, rows: list[MemoryRow], limit: int, strict_literal: bool) -> int:
    marker_counts, source_counts, classification_counts, hits = audit(rows)
    fail_classes = failure_classifications(strict_literal)
    failure_hit_count = sum(1 for hit in hits if hit.classification in fail_classes)

    print(f"DB path: {db_path}")
    print(f"Total memories: {len(rows)}")
    print(f"Total sessions: {total_sessions(rows)}")
    print(f"Unique hit rows: {len(hits)}")
    print(f"Failure hit rows: {failure_hit_count}")
    print()

    print("Marker hit counts:")
    for marker in MARKERS:
        print(f"  {marker}: {marker_counts[marker]}")
    print()

    print("Unique hit rows by classification:")
    for classification in (
        AUTO_META_CLASSIFICATION,
        INTERNAL_CONTEXT_CLASSIFICATION,
        LITERAL_DISCUSSION_CLASSIFICATION,
    ):
        print(f"  {classification}: {classification_counts[classification]}")
    print()

    print("Fail-on-hit classifications:")
    for classification in (
        AUTO_META_CLASSIFICATION,
        INTERNAL_CONTEXT_CLASSIFICATION,
        LITERAL_DISCUSSION_CLASSIFICATION,
    ):
        included = classification in fail_classes
        print(f"  {classification}: {'yes' if included else 'no'}")
    print()

    print("Hit counts by source kind:")
    for kind in ("claude", "codex", "opencode", "unknown"):
        print(f"  {kind}: {source_counts[kind]}")
    print()

    print(f"Samples (max {limit}):")
    for hit in hits[:limit]:
        row = hit.row
        source = transcript_path(row.metadata) or "<unknown>"
        chunk_index = row.metadata.get("chunk_index", "<unknown>")
        print(f"- id: {row.row_id}")
        print(f"  session_id: {row.session_id}")
        print(f"  classification: {hit.classification}")
        print(f"  source_kind: {source_kind(source)}")
        print(f"  source: {source}")
        print(f"  created_at: {row.created_at}")
        print(f"  chunk_index: {chunk_index}")
        print(f"  markers: {', '.join(hit.markers)}")
        print(f"  chunk_text: {sample_text(row.chunk_text)}")
    if not hits:
        print("- none")

    return failure_hit_count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit saved kasane memories for known noisy transcript markers."
    )
    parser.add_argument("--db-path", help="Path to the SQLite memory database.")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of sample rows to print. Default: 5.",
    )
    parser.add_argument(
        "--fail-on-hit",
        action="store_true",
        help=(
            "Exit with status 1 if any auto_meta or internal_context hit is found. "
            "literal_discussion is ignored unless --strict-literal is set."
        ),
    )
    parser.add_argument(
        "--strict-literal",
        action="store_true",
        help="Treat literal_discussion hits as failures when --fail-on-hit is set.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.limit < 0:
        print("--limit must be non-negative", file=sys.stderr)
        return 2

    db_path = resolve_db_path(args.db_path)
    rows = load_rows(db_path)
    failure_hit_count = print_report(db_path, rows, args.limit, args.strict_literal)
    if args.fail_on_hit and failure_hit_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
