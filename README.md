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

### おすすめの使い方

#### Claude Code ユーザー

- セッション終了時に hook で `save` を呼んで記憶を保存する
- 必要なときに `search` または `/memory` で過去の記憶を検索する

#### Codex ユーザー

- `watch-codex` を常駐させて `~/.codex/sessions` から記憶を自動取り込みする
- 必要なときに MCP ツール `search_memories` で過去の記憶を検索する
- 継続作業や以前の方針確認では、作業の早い段階で `kasane` を引く運用にする
- 特におすすめなのは、workspace 親フォルダの `AGENTS.md` に「継続タスクでは開始時に `kasane` を参照する」ルールを書くこと

#### 共通

- 保存先 DB は共通なので、Claude と Codex の両方の記憶を同じストアで検索できる
- 単発タスクでは無理に検索せず、継続性がある依頼で優先して使う
- たまに `stats` や `optimize` を実行して状態を確認する

#### 実運用のイメージ

- Claude では session 終了時に自動保存し、必要なときだけ検索する
- Codex では `watch-codex` を常駐させて自動保存し、継続タスクの開始時に `kasane` を参照する
- Codex での実運用は、親 workspace の `AGENTS.md` に参照ルールを書いて自動的にその判断をさせる形が扱いやすい
- 「前回の続き」「以前決めた方針」「過去に触った設定の再開」のような依頼で特に効果が高い
- 毎回必ず検索するのではなく、過去の文脈が効きそうな場面で優先して使う

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
