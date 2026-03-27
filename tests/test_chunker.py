from pathlib import Path

from kasane import chunker
from kasane.chunker import (
    MemoryChunk,
    _create_qa_pairs,
    _extract_codex_session_info,
    _generate_session_id,
    _load_codex_messages,
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


def test_load_codex_messages(tmp_path):
    jsonl_file = tmp_path / "codex.jsonl"
    jsonl_file.write_text(
        '{"type":"session_meta","payload":{"id":"session-1","timestamp":"2026-03-27T13:03:51.499Z"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"kasane を Codex でも使いたい"}]}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"できます。"}]}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"ignore me"}]}}\n',
        encoding="utf-8",
    )
    messages = _load_codex_messages(jsonl_file)
    assert messages == [
        {"role": "user", "content": "kasane を Codex でも使いたい"},
        {"role": "assistant", "content": "できます。"},
    ]


def test_extract_codex_session_info(tmp_path):
    jsonl_file = tmp_path / "codex.jsonl"
    jsonl_file.write_text(
        '{"type":"session_meta","payload":{"id":"session-abc","timestamp":"2026-03-27T13:03:51.499Z"}}\n',
        encoding="utf-8",
    )
    session_id, created_at = _extract_codex_session_info(jsonl_file)
    assert session_id == "session-abc"
    assert created_at.isoformat().startswith("2026-03-27T13:03:51.499")


def test_parse_codex_transcript_integration(tmp_path):
    jsonl_file = tmp_path / "codex.jsonl"
    jsonl_file.write_text(
        '{"type":"session_meta","payload":{"id":"session-xyz","timestamp":"2026-03-27T13:03:51.499Z"}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"前に決めた方針は？"}]}}\n'
        '{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"watcher 方式にします。"}]}}\n',
        encoding="utf-8",
    )
    chunks, _ = chunker.parse_codex_transcript(jsonl_file)
    assert len(chunks) == 1
    assert chunks[0].session_id == "session-xyz"
    assert "Q: 前に決めた方針は？" in chunks[0].chunk_text
    assert "A: watcher 方式にします。" in chunks[0].chunk_text
