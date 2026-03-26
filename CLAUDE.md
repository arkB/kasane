# kasane — Claude Code 長期記憶システム

## プロジェクト概要

kasane は Claude Code のセッション間で会話の記憶を永続化するシステムである。
セッション終了時に会話ログを自動保存し、次回以降のセッションで関連する過去の文脈を検索・取得できる。

## 設計原則（厳守）

1. **外部サービスに依存しない** — SQLite の 1 ファイルに全データを格納する。外部 DB・外部 API は使わない
2. **バックグラウンドでトークンを消費しない** — 記憶の保存に LLM を使わない。要約・圧縮は行わない
3. **セッション終了時に自動保存** — ユーザーの手動操作ゼロ。Hook で発火する

## 技術スタック

- **言語**: Python
- **依存パッケージ**: `sentence-transformers`, `sqlite-vec` の 2 つのみ（CLI は標準ライブラリの argparse を使用）
- **パッケージ管理**: uv（`uv sync` で完結）
- **埋め込みモデル**: Ruri v3-310M（日本語特化、CPU 動作可能）
- **データベース**: SQLite（FTS5 + sqlite-vec 拡張）

## アーキテクチャ

### データフロー（保存）

```
セッション終了
  → Hook 発火（settings.json の PostToolUse / Stop）
  → Claude Code が transcript を JSONL ファイルとして出力
  → kasane がファイルパスを受け取る
  → JSONL をパースし、Q&A ペア単位のチャンクに分割（ルールベース）
  → Ruri v3 でベクトル化（passage: prefix 付き）
  → SQLite に保存（テキスト + ベクトル + メタデータ）
```

### データフロー（検索・取得）

```
セッション開始 or ユーザーが明示的に検索
  → CLI の search コマンド実行
  → キーワード検索（FTS5）+ ベクトル検索（sqlite-vec、query: prefix 付き）
  → RRF + 時間減衰で統合
  → 上位 N 件のチャンクテキストを標準出力に返す（所定のフォーマット）
  → CLAUDE.md 内のカスタムスラッシュコマンド定義により
    Claude Code のコンテキストに注入される
```

### Claude Code への統合方法

記憶の取得は以下のいずれかで行う（併用可）。

1. **カスタムスラッシュコマンド（推奨）**
   利用先プロジェクトの CLAUDE.md に以下を記載し `/memory` コマンドを定義する:
   ```
   ## カスタムコマンド
   /memory <query> — kasane を検索して関連する過去の会話を取得する。
   実行: cd /path/to/kasane && uv run python -m kasane.main search --query "<query>"
   ```
   ユーザーが `/memory Tailscale設定` と打つと、関連する過去の文脈がコンテキストに入る。

2. **Hook による自動注入（実験的）**
   セッション開始時の Hook でプロジェクト名等をクエリにして自動検索し、
   結果をシステムプロンプトに追加する方式。ただし Claude Code の Hook API の
   対応状況に依存するため、まずはスラッシュコマンド方式で実装する。

### ディレクトリ構成

```
kasane/
├── .gitignore                 # memory.db, __pycache__, .venv 等
├── pyproject.toml
├── README.md
├── src/
│   └── kasane/
│       ├── __init__.py
│       ├── main.py            # CLI エントリポイント（save / search / stats / warmup）
│       ├── chunker.py          # transcript JSONL → Q&A チャンク分割
│       ├── embedder.py         # Ruri v3 によるベクトル化
│       ├── storage.py          # SQLite 読み書き（FTS5 + sqlite-vec）
│       └── search.py           # ハイブリッド検索 + RRF 統合
├── tests/
│   ├── fixtures/              # テスト用サンプル transcript
│   │   └── sample_transcript.jsonl
│   ├── test_chunker.py
│   ├── test_search.py
│   └── test_storage.py
└── data/
    └── memory.db               # SQLite データファイル（gitignore 対象）
```

### データベーススキーマ

```sql
-- メインテーブル: チャンクの実体
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    created_at DATETIME NOT NULL,   -- セッション実施日時（明示的に指定、DEFAULT 不使用）
    metadata JSON                   -- 拡張用（後述）
);

-- セッション単位の存在確認用インデックス
CREATE INDEX idx_memories_session_id ON memories(session_id);

-- FTS5 全文検索インデックス（trigram トークナイザ）
CREATE VIRTUAL TABLE memories_fts USING fts5(
    chunk_text,
    content='memories',
    content_rowid='id',
    tokenize='trigram'
);

-- FTS5 同期トリガー
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, chunk_text)
    VALUES('delete', old.id, old.chunk_text);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, chunk_text)
    VALUES('delete', old.id, old.chunk_text);
    INSERT INTO memories_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
END;

-- sqlite-vec ベクトルインデックス
CREATE VIRTUAL TABLE memories_vec USING vec0(
    id INTEGER PRIMARY KEY,
    embedding FLOAT[768]          -- Ruri v3-310M の次元数
);
```

#### memories テーブルと memories_vec の ID 同期

memories_vec の id は memories テーブルの id と一致させる必要がある。
insert 時は以下の手順を 1 トランザクション内で行う:

```python
cursor.execute("INSERT INTO memories (...) VALUES (...)")
row_id = cursor.lastrowid  # AUTOINCREMENT で採番された ID
cursor.execute(
    "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
    (row_id, embedding_bytes),
)
```

#### 二重保存防止

保存処理の冒頭で `SELECT 1 FROM memories WHERE session_id = ? LIMIT 1` を実行し、存在すれば保存処理全体をスキップする。Hook は 1 セッションにつき 1 回だけ発火する設計のため、アプリケーション層のチェックで十分であり、DB 制約によるフォールバックは設けない。

#### created_at のセマンティクス

`created_at` はセッションが実施された日時を格納する。`DEFAULT CURRENT_TIMESTAMP` は使わない。
値はtranscript ファイルの更新日時（`os.path.getmtime`）、またはファイル内に timestamp フィールドがあればその値を使用する。理由は以下の通り:

- 時間減衰がこの値に依存するため、INSERT 時刻ではなくセッション実施時刻である必要がある
- 過去の transcript を一括インポートした場合、INSERT 時刻では全て同一日時になり時間減衰が機能しない
- Hook が遅延した場合にもセッション時刻を正確に保持できる

#### metadata JSON カラムの仕様

現時点では拡張用として確保する。初期実装では以下のフィールドを格納する:

```json
{
  "transcript_path": "/path/to/original/transcript.jsonl",
  "chunk_index": 0
}
```

- `transcript_path`: 元の transcript ファイルのパス。デバッグやトレーサビリティ用
- `chunk_index`: セッション内でのチャンクの順番（0-indexed）。文脈の前後関係を復元する際に使用

将来的にプロジェクト名やタグなどを追加する可能性があるが、初期実装では上記のみとする。

## 主要モジュール仕様

### chunker.py — チャンク分割

- 入力: Claude Code が出力する transcript ファイル（JSONL 形式）
  - 各行が JSON オブジェクト。`role`（"human" / "assistant"）と `content` フィールドを持つ
  - Claude Code の実際の出力形式に合わせること。初回実装時に実ファイルで確認する
- 出力: Q&A ペアのチャンクリスト
- チャンク単位:
  - Human の発話 + それに対する Assistant の応答 = 1 チャンク（1 レコード）
  - chunk_text には両方を結合して格納する（例: `"Q: ...\nA: ..."` 形式）
  - 長すぎるペアは意味的な区切り（空行、トピック変化）で分割
  - **分割上限: 1 チャンクあたり約 2,000 文字**（日本語は 1 文字 ≈ 1〜2 トークンのため、概ね 1,000〜2,000 トークン相当。トークナイザを追加せず文字数で近似する）
- ルール:
  - LLM は使わない。正規表現とヒューリスティクスのみ
  - トークナイザ（tiktoken 等）も追加しない。文字数カウントで代用する
  - 各チャンクにセッション ID、作成日時、chunk_index をメタデータとして付与

### embedder.py — ベクトル化

- モデル: `cl-nagoya/ruri-v3-310m`（sentence-transformers で読み込み）
- 日本語テキストに最適化された 310M パラメータの小型モデル
- CPU で十分な速度で動作する（GPU 不要）
- バッチ処理でチャンクをまとめてベクトル化
- ベクトル次元: 1024
- **prefix 仕様（必須）**:
  - 検索クエリには `"query: "` prefix を付与する
  - 保存するチャンクテキストには `"passage: "` prefix を付与する
  - prefix を付けないとベクトル検索の精度が大幅に低下する
- `normalize_embeddings=True` を指定する（コサイン類似度に必要）
- モデルのシングルトン化:
  - モジュールレベルで遅延ロードし、複数回の呼び出しでモデルを再ロードしない

### storage.py — データ永続化

- SQLite 単一ファイルに全データを格納
- sqlite-vec 拡張でベクトルインデックスを管理
- FTS5（trigram トークナイザ）で全文検索インデックスを管理
- FTS5 同期トリガーをスキーマ初期化に含める（上記スキーマ参照）
- トランザクション単位: 1 セッション分のチャンクを一括 insert
- memories と memories_vec の ID 同期は `lastrowid` で行う（上記手順参照）
- 二重保存防止: session_id の存在チェック → 存在すれば保存処理全体をスキップ
- created_at にはセッション実施日時を明示的に指定する（DEFAULT 不使用）
- data/memory.db は .gitignore に追加すること

### search.py — ハイブリッド検索

#### 2 種類の検索

1. **キーワード検索（FTS5 trigram）**
   - SQLite FTS5 の trigram トークナイザを使用
   - 形態素解析（MeCab 等）不要で日本語を検索可能
   - 固有名詞（Tailscale、LaunchAgent 等）に強い
   - 3 文字単位で文字列を分割するシンプルな方式

2. **ベクトル検索（sqlite-vec）**
   - クエリに `"query: "` prefix を付けて Ruri v3 でベクトル化
   - 意味的に近いチャンクを検索
   - 固有名詞には弱いがキーワード検索が補完する

#### 検索結果の統合: RRF（Reciprocal Rank Fusion）

```python
def rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank)
```

- 各検索結果の順位からスコアを算出し合算
- 検索方式ごとのスコアの単位が異なっても公平に扱える
- k=60 は標準的な値（調整可能）

#### 時間減衰（Time Decay）

```python
import math

def time_decay(days_old: float, half_life: float = 30.0) -> float:
    return math.pow(0.5, days_old / half_life)
```

- 半減期: 30 日
- 30 日前の記憶 → スコア × 0.5
- 60 日前の記憶 → スコア × 0.25
- RRF スコアに時間減衰を乗算して最終スコアとする

#### 検索フロー

```
クエリ入力
  → キーワード検索（FTS5）→ 結果リスト A
  → ベクトル検索（sqlite-vec、query: prefix 付き）→ 結果リスト B
  → RRF で A と B を統合
  → 時間減衰を適用
  → 上位 N 件を返却
```

## Claude Code 設定（settings.json）

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

> **注意**: `$CLAUDE_TRANSCRIPT` は Claude Code が Hook に渡す環境変数の想定名。
> 実際の変数名は Claude Code のドキュメントで確認し、正しい名前に置き換えること。
> Hook API の仕様が変わった場合はここを更新する。

## CLI インターフェース

CLI は標準ライブラリの argparse で実装する（click 等の追加依存は使わない）。

```bash
# モデルの事前ダウンロード（初回セットアップ時に必ず実行）
uv run python -m kasane.main warmup

# 記憶の保存（通常は Hook から自動実行）
uv run python -m kasane.main save --transcript /path/to/transcript.jsonl

# 記憶の検索（カスタムスラッシュコマンド or 手動実行）
uv run python -m kasane.main search --query "Tailscale の設定方法"

# top_k を変更する場合（デフォルト: 5）
uv run python -m kasane.main search --query "Tailscale の設定方法" --top-k 10

# 統計情報の表示
uv run python -m kasane.main stats

# データベースの最適化
uv run python -m kasane.main optimize
```

### search コマンドの出力フォーマット

search コマンドは以下の形式で標準出力に結果を出力する。
カスタムスラッシュコマンド経由で Claude Code のコンテキストに注入されるため、
LLM が読みやすいプレーンテキスト形式とする。

```
[1/5] score=0.0312 | 2025-03-15 | session=a1b2c3d4
Q: Tailscale の設定で困っている。サブネットルータの設定方法は？
A: tailscale up --advertise-routes=192.168.1.0/24 でサブネットを広告できます...
---
[2/5] score=0.0287 | 2025-03-10 | session=e5f6g7h8
Q: VPN の選定について相談したい
A: Tailscale は WireGuard ベースで設定が簡単です...
---
（以下同様）
```

- 各結果はヘッダー行（順位、スコア、日時、セッション ID）とチャンクテキストで構成
- 結果間は `---` で区切る
- 結果が 0 件の場合は `No memories found for: <query>` と出力する
- デフォルトの top_k は 5

### warmup コマンド

初回セットアップ時に `warmup` を実行して Ruri v3 モデルを事前ダウンロードする。
モデルは約 1.2GB あり、ダウンロードに数分かかる。
Hook の timeout（30 秒）内にモデルダウンロード + ベクトル化を完了するのは不可能なため、
**初回の `uv sync` の直後に `warmup` を実行すること**をセットアップ手順に含める。

## CLAUDE.md との棲み分け

| 項目 | CLAUDE.md | kasane |
|------|-----------|------------|
| 役割 | プロジェクトのルールブック | 過去の会話の蓄積 |
| 内容 | 技術スタック、コーディング規約、構成 | 設計判断の経緯、失敗の記録、議論の文脈 |
| 性質 | 静的（セッション間で不変） | 動的（セッションごとに蓄積） |
| 例え | 取扱説明書 | 共有した経験 |

両方あって初めて「文脈を持った壁打ち相手」になる。

## コーディング規約

- 型ヒントを全関数に付与する
- docstring は日本語で書く
- エラーハンドリング: 保存失敗時もセッション終了をブロックしない（try-except で握りつぶさず warning をログ出力）
- ログ: `logging` モジュールを使用。デフォルト INFO レベル
- テスト: pytest を使用。主要モジュールごとにテストファイルを作成

## パフォーマンス目標

- 検索レスポンス: 100ms 以下（モデルロード済みの状態）
- 保存処理: セッション終了から 30 秒以内に完了（モデルは warmup で事前ダウンロード済み前提）
- メモリ使用: Ruri v3 モデルロード込みで 2GB 以下

## 注意事項

- memory.db は個人データを含むため、絶対に git に commit しない
- OpenAI Embeddings API 等の外部 API は使わない（コスト + プライバシー）
- LLM による要約は行わない（重要な文脈が消えるリスクを避ける）
- 何を残して何を捨てるかの判断は、保存時ではなく検索時に行う
- 初回セットアップ時に `warmup` コマンドでモデルを事前ダウンロードすること
