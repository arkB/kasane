import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import MemoryChunk

logger = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 2000


def parse_transcript(file_path: str | Path) -> tuple[list[MemoryChunk], datetime]:
    file_path = Path(file_path)
    session_id = _generate_session_id(file_path.name)
    created_at = _extract_created_at(file_path)
    messages = _load_messages(file_path)
    pairs = _create_qa_pairs(messages)
    chunks = _split_into_chunks(pairs, session_id, created_at, str(file_path))
    logger.info(f"Parsed {len(chunks)} chunks from {file_path}")
    return chunks, created_at


def _generate_session_id(filename: str) -> str:
    return hashlib.sha256(filename.encode()).hexdigest()[:16]


def _extract_created_at(file_path: Path) -> datetime:
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime)


def _load_messages(file_path: Path) -> list[dict[str, str]]:
    messages = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON at line {line_num}: {e}")
                continue
            role = obj.get("role", obj.get("type", ""))
            content = obj.get("content", obj.get("message", ""))
            if isinstance(content, list):
                content_parts = []
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        content_parts.append(item["text"])
                    elif isinstance(item, str):
                        content_parts.append(item)
                content = "\n".join(content_parts)
            if role and content:
                messages.append({"role": role, "content": str(content)})
    return messages


def _create_qa_pairs(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    pairs = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"].lower()
        if role in ("human", "user"):
            human_text = msg["content"]
            assistant_text = ""
            j = i + 1
            while j < len(messages):
                next_role = messages[j]["role"].lower()
                if next_role in ("human", "user"):
                    break
                if next_role in ("assistant", "ai"):
                    assistant_text = messages[j]["content"]
                    j += 1
                    break
                j += 1
            pairs.append({"human": human_text, "assistant": assistant_text})
            i = j
        else:
            i += 1
    return pairs


def _split_into_chunks(
    pairs: list[dict[str, str]],
    session_id: str,
    created_at: datetime,
    transcript_path: str,
) -> list[MemoryChunk]:
    chunks = []
    chunk_index = 0
    for pair in pairs:
        human_text = pair["human"]
        assistant_text = pair["assistant"]
        combined = f"Q: {human_text}\nA: {assistant_text}"
        if len(combined) <= MAX_CHUNK_CHARS:
            chunks.append(
                MemoryChunk(
                    session_id=session_id,
                    chunk_text=combined,
                    created_at=created_at,
                    metadata={
                        "transcript_path": transcript_path,
                        "chunk_index": chunk_index,
                    },
                )
            )
            chunk_index += 1
        else:
            sub_chunks = _split_long_text(
                human_text, assistant_text, session_id, created_at, transcript_path
            )
            for idx, sub_chunk in enumerate(sub_chunks):
                sub_chunk.metadata["chunk_index"] = chunk_index + idx
                chunks.append(sub_chunk)
            chunk_index += len(sub_chunks)
    return chunks


def _split_long_text(
    human_text: str,
    assistant_text: str,
    session_id: str,
    created_at: datetime,
    transcript_path: str,
) -> list[MemoryChunk]:
    chunks = []
    human_parts = _split_by_boundaries(human_text, "human")
    assistant_parts = _split_by_boundaries(assistant_text, "assistant")
    n_parts = max(len(human_parts), len(assistant_parts))
    for i in range(n_parts):
        h_part = human_parts[i] if i < len(human_parts) else ""
        a_part = assistant_parts[i] if i < len(assistant_parts) else ""
        combined = f"Q: {h_part}\nA: {a_part}"
        chunks.append(
            MemoryChunk(
                session_id=session_id,
                chunk_text=combined,
                created_at=created_at,
                metadata={"transcript_path": transcript_path, "chunk_index": 0},
            )
        )
    return chunks


def _split_by_boundaries(text: str, context: str = "") -> list[str]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    paragraphs = text.split("\n\n")
    parts = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > MAX_CHUNK_CHARS:
            if current:
                parts.append(current.strip())
            current = para
        else:
            if current:
                current += "\n\n" + para
            else:
                current = para
    if current:
        parts.append(current.strip())
    if not parts:
        parts = [
            text[i : i + MAX_CHUNK_CHARS] for i in range(0, len(text), MAX_CHUNK_CHARS)
        ]
    return parts
