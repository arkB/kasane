import json
import sys
from typing import Any

from kasane import search
from kasane import storage


SERVER_INFO = {"name": "kasane", "version": "0.1.0"}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

SEARCH_TOOL = {
    "name": "search_memories",
    "description": "Search saved kasane memories and return the most relevant past conversation chunks.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query in Japanese or English.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
                "description": "Maximum number of memories to return.",
            },
        },
        "required": ["query"],
    },
}

STATS_TOOL = {
    "name": "memory_stats",
    "description": "Show kasane database statistics.",
    "inputSchema": {"type": "object", "properties": {}},
}


def _read_message() -> dict[str, Any] | None:
    content_length: int | None = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        header = line.decode("utf-8").strip()
        if header.lower().startswith("content-length:"):
            content_length = int(header.split(":", 1)[1].strip())
    if content_length is None:
        raise ValueError("Missing Content-Length header")
    payload = sys.stdin.buffer.read(content_length)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _error_response(message_id: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def _format_search_results(query: str, top_k: int) -> str:
    storage.init_db()
    results = search.hybrid_search(query, top_k=top_k)
    if not results:
        return f"No memories found for: {query}"

    lines: list[str] = []
    for index, result in enumerate(results, start=1):
        created_at_str = result.created_at.strftime("%Y-%m-%d")
        lines.append(
            f"[{index}/{len(results)}] score={result.score:.4f} | {created_at_str} | session={result.session_id}"
        )
        lines.append(result.chunk_text)
        if index < len(results):
            lines.append("---")
    return "\n".join(lines)


def _format_stats() -> str:
    storage.init_db()
    stats = storage.get_stats()
    db_size_mb = stats["db_size_bytes"] / (1024 * 1024)
    return "\n".join(
        [
            f"Total memories: {stats['total_memories']}",
            f"Total sessions: {stats['total_sessions']}",
            f"Database size: {db_size_mb:.2f} MB",
        ]
    )


def _handle_initialize(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    protocol_version = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        },
    }


def _handle_tools_call(message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if name == "search_memories":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error_response(message_id, -32602, "query must be a non-empty string")
        top_k = arguments.get("top_k", 5)
        if not isinstance(top_k, int):
            return _error_response(message_id, -32602, "top_k must be an integer")
        text = _format_search_results(query=query.strip(), top_k=max(1, min(top_k, 20)))
    elif name == "memory_stats":
        text = _format_stats()
    else:
        return _error_response(message_id, -32601, f"Unknown tool: {name}")

    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def _handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        return _handle_initialize(message_id, params)
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"tools": [SEARCH_TOOL, STATS_TOOL]},
        }
    if method == "tools/call":
        try:
            return _handle_tools_call(message_id, params)
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "content": [{"type": "text", "text": f"kasane error: {exc}"}],
                    "isError": True,
                },
            }
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"resources": []}}

    return _error_response(message_id, -32601, f"Method not found: {method}")


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            break
        response = _handle_request(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    main()
