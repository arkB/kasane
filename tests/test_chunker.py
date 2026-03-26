from pathlib import Path

from kasane import chunker
from kasane.chunker import (
    MemoryChunk,
    _create_qa_pairs,
    _generate_session_id,
    _load_messages,
    _split_into_chunks,
)


def test_generate_session_id():
    sid = _generate_session_id("test_transcript.jsonl")
    assert len(sid) == 16
    assert sid.isalnum()


def test_load_messages(tmp_path):
    jsonl_file = tmp_path / "test.jsonl"
    jsonl_file.write_text(
        '{"role": "human", "content": "Hello"}\n'
        '{"role": "assistant", "content": "Hi there"}\n'
        '{"role": "human", "content": "How are you?"}\n'
        '{"role": "assistant", "content": "I am fine"}\n',
        encoding="utf-8",
    )
    messages = _load_messages(jsonl_file)
    assert len(messages) == 4
    assert messages[0]["role"] == "human"
    assert messages[0]["content"] == "Hello"


def test_create_qa_pairs():
    messages = [
        {"role": "human", "content": "Question 1"},
        {"role": "assistant", "content": "Answer 1"},
        {"role": "human", "content": "Question 2"},
        {"role": "assistant", "content": "Answer 2"},
    ]
    pairs = _create_qa_pairs(messages)
    assert len(pairs) == 2
    assert pairs[0]["human"] == "Question 1"
    assert pairs[0]["assistant"] == "Answer 1"


def test_create_qa_pairs_handles_user_role():
    messages = [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "Answer"},
    ]
    pairs = _create_qa_pairs(messages)
    assert len(pairs) == 1
    assert pairs[0]["human"] == "Question"


def test_split_into_chunks():
    pairs = [{"human": "Q?", "assistant": "A."}]
    chunks = _split_into_chunks(
        pairs, "sid", chunker._extract_created_at(Path(__file__)), "/tmp/test.jsonl"
    )
    assert len(chunks) == 1
    assert "Q: Q?" in chunks[0].chunk_text
    assert "A: A." in chunks[0].chunk_text
    assert chunks[0].metadata["transcript_path"] == "/tmp/test.jsonl"
    assert chunks[0].metadata["chunk_index"] == 0


def test_parse_transcript_integration(tmp_path):
    jsonl_file = tmp_path / "sample.jsonl"
    jsonl_file.write_text(
        '{"role": "human", "content": "Test question?"}\n'
        '{"role": "assistant", "content": "Test answer."}\n',
        encoding="utf-8",
    )
    chunks, created_at = chunker.parse_transcript(jsonl_file)
    assert len(chunks) == 1
    assert "Q: Test question?" in chunks[0].chunk_text
    assert "A: Test answer." in chunks[0].chunk_text
