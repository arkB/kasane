import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterable
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
    transcript_mtime = os.path.getmtime(file_path)
    messages = _load_messages(file_path)
    pairs = _create_qa_pairs(messages)
    chunks = _split_into_chunks(
        pairs,
        session_id,
        created_at,
        str(file_path),
        transcript_mtime=transcript_mtime,
    )
    logger.info(f"Parsed {len(chunks)} chunks from {file_path}")
    return chunks, created_at


def parse_codex_transcript(file_path: str | Path) -> tuple[list[MemoryChunk], datetime]:
    file_path = Path(file_path)
    session_id, created_at = _extract_codex_session_info(file_path)
    transcript_mtime = os.path.getmtime(file_path)
    messages = _load_codex_messages(file_path)
    pairs = _create_qa_pairs(messages)
    chunks = _split_into_chunks(
        pairs,
        session_id,
        created_at,
        str(file_path),
        transcript_mtime=transcript_mtime,
    )
    logger.info(f"Parsed {len(chunks)} Codex chunks from {file_path}")
    return chunks, created_at


def parse_opencode_session(
    db_path: str | Path, session_id: str
) -> tuple[list[MemoryChunk], datetime]:
    db_path = Path(db_path)
    session_meta, messages = _load_opencode_session(db_path, session_id)
    created_at = datetime.fromtimestamp(session_meta["time_created"] / 1000)
    session_updated = session_meta["time_updated"] / 1000
    pairs = _create_qa_pairs(messages)
    transcript_path = f"opencode-db:{db_path}#{session_id}"
    chunks = _split_into_chunks(
        pairs,
        session_id,
        created_at,
        transcript_path,
        transcript_mtime=session_updated,
    )
    logger.info(f"Parsed {len(chunks)} OpenCode chunks from session {session_id}")
    return chunks, created_at


def _generate_session_id(filename: str) -> str:
    return hashlib.sha256(filename.encode()).hexdigest()[:16]


def _extract_created_at(file_path: Path) -> datetime:
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime)


def _parse_iso8601(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _extract_codex_session_info(file_path: Path) -> tuple[str, datetime]:
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
            if obj.get("type") != "session_meta":
                continue
            payload = obj.get("payload", {})
            session_id = payload.get("id")
            timestamp = payload.get("timestamp")
            if not session_id:
                break
            if isinstance(timestamp, str):
                return str(session_id), _parse_iso8601(timestamp)
            break
    return _generate_session_id(file_path.name), _extract_created_at(file_path)


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


def _join_content_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable):
        content_parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    content_parts.append(text)
            elif isinstance(item, str) and item:
                content_parts.append(item)
        return "\n".join(content_parts)
    return ""


def _load_codex_messages(file_path: Path) -> list[dict[str, str]]:
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
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            content = _join_content_parts(payload.get("content", []))
            if content:
                messages.append({"role": str(role), "content": content})
    return messages


def _load_opencode_session(
    db_path: Path, session_id: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, directory, time_created, time_updated FROM session WHERE id = ?",
        (session_id,),
    )
    session_row = cursor.fetchone()
    if session_row is None:
        conn.close()
        raise ValueError(f"OpenCode session not found: {session_id}")

    cursor.execute(
        """
        SELECT
            m.id,
            m.time_created,
            m.data,
            p.time_created,
            p.data
        FROM message m
        LEFT JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ?
        ORDER BY m.time_created ASC, p.time_created ASC
        """,
        (session_id,),
    )

    message_map: dict[str, dict[str, Any]] = {}
    message_order: list[str] = []
    for message_id, _message_time, message_data_raw, _part_time, part_data_raw in cursor.fetchall():
        if message_id not in message_map:
            message_data = json.loads(message_data_raw)
            message_map[message_id] = {
                "role": message_data.get("role", ""),
                "parts": [],
            }
            message_order.append(message_id)
        if part_data_raw:
            message_map[message_id]["parts"].append(json.loads(part_data_raw))
    conn.close()

    messages = []
    for message_id in message_order:
        message = message_map[message_id]
        role = str(message["role"])
        if role not in ("user", "assistant"):
            continue
        text_parts = [
            part["text"]
            for part in message["parts"]
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part.get("text")
        ]
        content = "\n".join(text_parts).strip()
        if content:
            messages.append({"role": role, "content": content})

    return {
        "id": session_row[0],
        "directory": session_row[1],
        "time_created": session_row[2],
        "time_updated": session_row[3],
    }, messages


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
    transcript_mtime: float | None = None,
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
                        "transcript_mtime": transcript_mtime,
                    },
                )
            )
            chunk_index += 1
        else:
            sub_chunks = _split_long_text(
                human_text,
                assistant_text,
                session_id,
                created_at,
                transcript_path,
                transcript_mtime=transcript_mtime,
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
    transcript_mtime: float | None = None,
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
                metadata={
                    "transcript_path": transcript_path,
                    "chunk_index": 0,
                    "transcript_mtime": transcript_mtime,
                },
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
