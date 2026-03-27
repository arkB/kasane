# kasane — Claude Code / Codex / OpenCode 向け長期記憶システム

Claude Code / Codex / OpenCode のセッション間で会話の記憶を永続化するシステム。

## セットアップ

```bash
# 依存パッケージのインストール
uv sync

# モデルの事前ダウンロード（初回必須、約1.2GB）
uv run python -m kasane.main warmup
```

## 使い方

### 使い方は 2 種類ある

1. ユーザーが明示的に `kasane` を使って過去の記憶を検索する
2. Claude Code / Codex / OpenCode 自体が、継続タスクの開始時に `kasane` を参照する

### 1. ユーザーが `kasane` を使う場合

#### 共通

- CLI では `search` で検索する
- DB は共通なので、Claude Code / Codex / OpenCode の記憶を横断して引ける

```bash
# 基本検索（デフォルト top_k=5）
uv run python -m kasane.main search --query "Tailscale の設定方法"

# 結果数を変更
uv run python -m kasane.main search --query "Python 非同期処理" --top-k 10
```

#### Claude Code

- `CLAUDE.md` に `/memory` を定義しておくと使いやすい

```text
## カスタムコマンド
/memory <query> — kasane を検索して関連する過去の会話を取得する。
実行: cd /path/to/kasane && uv run python -m kasane.main search --query "<query>"
```

#### Codex

- MCP サーバー `src/kasane/mcp_server.py` 経由で `search_memories` と `memory_stats` を使う

#### OpenCode

- `kasane search` を直接呼ぶか、custom command `/memory` を作って呼ぶ

### 2. エージェント自体が `kasane` を使う場合

#### 共通

- 継続タスク、以前決めた方針の確認、過去に触った設定の再開のような依頼で、作業開始時に `kasane` を参照する運用にする
- 単発タスクでは無理に検索しない
- 保存は自動化し、参照ルールは各エージェントの instruction ファイルに書くのがおすすめ

#### Claude Code

- 保存は session 終了時の hook で行う
- 親 workspace の `CLAUDE.md` に「継続タスクでは開始時に `kasane` を参照する」ルールを書くのがおすすめ

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

#### Codex

- 既存の履歴は `import-codex` で一括取り込みする
- 常用時の自動保存は統合 watcher `watch-all` で行う
- 親 workspace の `AGENTS.md` に「継続タスクでは開始時に `kasane` を参照する」ルールを書くのがおすすめ

```bash
# 既存の Codex セッションを一括取り込み
uv run python -m kasane.main import-codex

# 常駐監視では Codex / OpenCode を 1 プロセスでまとめて監視する
uv run python -m kasane.main watch-all --interval 30
```

#### OpenCode

- 既存の履歴は `import-opencode` で一括取り込みする
- 常用時の自動保存は統合 watcher `watch-all` で行う
- 親 workspace の `.opencode/agents/` や custom command に開始時参照ルールをまとめるのがおすすめ

```bash
# 既存の OpenCode session を一括取り込み
uv run python -m kasane.main import-opencode

# 常駐監視では Codex / OpenCode を 1 プロセスでまとめて監視する
uv run python -m kasane.main watch-all --interval 30
```

#### 常駐 watcher のおすすめ運用

- 初回セットアップ時だけ `import-codex` / `import-opencode` を流して過去履歴を取り込む
- 以後は `watch-all` を常駐させて、新しい更新だけを軽く追う
- `watch-all` は初回起動時に現在位置を seed するため、未取り込みの古い履歴まで自動で掘り返さない
- 更新中の session は一定時間 settle するまで保留するため、進行中の長い会話を毎回再埋め込みしない
- Codex と OpenCode は同一プロセスで監視するので、埋め込みモデルのメモリ常駐も 1 本で済む

### 補助コマンド

- 状態確認には `stats` を使う
- DB メンテナンスには `optimize` を使う

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
