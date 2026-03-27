# kasane — Claude Code 長期記憶システム

Claude Code のセッション間で会話の記憶を永続化するシステム。

## セットアップ

```bash
# 依存パッケージのインストール
uv sync

# モデルの事前ダウンロード（初回必須、約1.2GB）
uv run python -m kasane.main warmup
```

## 使い方

### Codex で使う

Codex からは `src/kasane/mcp_server.py` を MCP サーバーとして使う。

Codex 側で `kasane` を有効化すると、以下の MCP ツールが使える:

- `search_memories` - 過去の会話メモリを検索
- `memory_stats` - メモリ DB の統計を表示

Codex には Claude Code の `Stop` hook 相当がない前提で、保存は session JSONL の取り込みで行う:

```bash
# 既存の Codex セッションを一括取り込み
uv run python -m kasane.main import-codex

# 継続監視して新しい Codex セッションを自動取り込み
uv run python -m kasane.main watch-codex --interval 30
```

デフォルトでは `~/.codex/sessions` を読む。初回は埋め込みモデルがローカルに必要なので、事前に `warmup` を済ませておく。

### 記憶の検索

```bash
# 基本検索（デフォルト top_k=5）
uv run python -m kasane.main search --query "Tailscale の設定方法"

# 結果数を変更
uv run python -m kasane.main search --query "Python 非同期処理" --top-k 10
```

### 記憶の保存（自動）

Claude Code の settings.json に Hook を設定すると、セッション終了時に自動保存されます:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Stop",
        "command": "cd /path/to/kasane && uv run python -m kasane.main save --transcript \"$CLAUDE_TRANSCRIPT\"",
        "timeout": 30
      }
    ]
  }
}
```

### カスタムスラッシュコマンド

利用先プロジェクトの CLAUDE.md に以下を追記:

```
## カスタムコマンド
/memory <query> — kasane を検索して関連する過去の会話を取得する。
実行: cd /path/to/kasane && uv run python -m kasane.main search --query "<query>"
```

### 統計情報

```bash
uv run python -m kasane.main stats
```

### データベース最適化

```bash
uv run python -m kasane.main optimize
```

## アーキテクチャ

- **埋め込み**: Ruri v3-310M（日本語特化、ローカル実行）
- **データベース**: SQLite（FTS5 + sqlite-vec）
- **検索**: キーワード検索 + ベクトル検索のハイブリッド（RRF統合）
- **時間減衰**: 30日半減期

## 注意事項

- `data/memory.db` は個人データを含むため git に commit しない
- 外部 API（OpenAI Embeddings 等）は使用しない
- LLM による要約は行わない
