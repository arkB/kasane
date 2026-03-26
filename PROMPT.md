# PROMPT.md — kasane 実装ガイド

> このファイルは、Claude Code が CLAUDE.md の仕様に基づいて kasane を実装する際に参照するプロンプトである。
> 実装の順序、判断基準、よくある落とし穴をまとめている。

---

## 実装の進め方

### フェーズ 1: プロジェクト基盤（最初にやる）

```
1. .gitignore を作成する
   - data/memory.db
   - __pycache__/
   - .venv/
   - *.egg-info/

2. pyproject.toml を作成する
   - name: kasane
   - dependencies: sentence-transformers, sqlite-vec
   - Python >= 3.11
   - パッケージ管理は uv を使う

3. ディレクトリ構成を CLAUDE.md 通りに作成する
   - tests/fixtures/ ディレクトリも忘れずに作る

4. `uv sync` で依存パッケージをインストールし、import できることを確認する
```

**確認ポイント**: `uv run python -c "import sentence_transformers; import sqlite_vec"` が通ること。

### フェーズ 2: ストレージ層（storage.py）

データの読み書きが全ての基盤になる。ここから作る。

```
1. SQLite データベースの初期化処理を書く
   - sqlite-vec 拡張のロード
   - CLAUDE.md のスキーマ定義をそのまま使う（memories, memories_fts, memories_vec）
   - FTS5 同期トリガー（memories_ai, memories_ad, memories_au）も初期化に含める
   - session_id のインデックスを作成する

2. メモリの insert 関数を実装する
   - memories テーブルに INSERT → lastrowid を取得
   - 取得した lastrowid を使って memories_vec にも INSERT
   - FTS5 はトリガーで自動同期（手動 INSERT 不要）
   - 1 セッション分を 1 トランザクションで処理する
   - created_at にはセッション実施日時を明示的に渡す（DEFAULT 不使用）

3. 二重保存防止を実装する
   - 保存処理の冒頭で session_id の存在を SELECT で確認
     → 存在すれば保存処理全体をスキップ（ログに info 出力）
   - Hook は 1 セッション 1 回発火の設計のため、これで十分
   - DB 制約（UNIQUE INDEX 等）によるフォールバックは設けない

4. 検索用の低レベル関数を実装する
   - fts_search(query, limit) → [(id, rank), ...]
   - vec_search(embedding, limit) → [(id, distance), ...]
```

### フェーズ 3: 埋め込み層（embedder.py）

```
1. Ruri v3-310M モデルのロード処理を書く
   - モデル名: cl-nagoya/ruri-v3-310m
   - sentence_transformers.SentenceTransformer で読み込む
   - 初回起動時にダウンロード（キャッシュされる）
   - モジュールレベルで遅延ロード（シングルトン）

2. encode 関数を実装する
   - 単一テキスト → 1024 次元ベクトル
   - バッチテキスト → ベクトルのリスト
   - normalize_embeddings=True を指定する（コサイン類似度に必要）

3. prefix を必ず付与する
   - 保存時（チャンクのベクトル化）: テキストに "passage: " prefix を付ける
   - 検索時（クエリのベクトル化）: テキストに "query: " prefix を付ける
   - これを忘れるとベクトル検索の精度が大幅に低下する
   - encode 関数のインターフェース例:
     encode(text, prefix="passage") → 内部で f"{prefix}: {text}" としてからモデルに渡す
```

**重要**: Ruri v3 は query/passage の prefix を前提に学習されたモデルである。
prefix なしでは埋め込み空間が正しく機能しない。

### フェーズ 4: チャンク分割（chunker.py）

```
1. Claude Code の transcript フォーマットを確認する
   - JSONL 形式を想定（各行が {"role": "human"/"assistant", "content": "..."} の JSON）
   - 実装前に Claude Code が実際に出力する transcript ファイルを 1 つ取得して確認する
   - 想定と異なるフォーマットだった場合はパーサーを合わせる

2. Q&A ペアチャンク分割ロジックを実装する
   - 基本単位: Human の発話 + それに対する Assistant の応答 = 1 チャンク
   - chunk_text の形式: "Q: {human_text}\nA: {assistant_text}"
   - 分割上限: 1 チャンクあたり約 2,000 文字で分割する
     （日本語 1 文字 ≈ 1〜2 トークン相当。tiktoken 等のトークナイザは追加しない）
   - 分割の境界: 空行、見出し、トピック変化のヒューリスティクス

3. メタデータの付与
   - session_id: transcript ファイル名のハッシュ（SHA-256 の先頭 16 文字）で一意に生成
   - created_at: セッション実施日時（ファイルの更新日時 os.path.getmtime、
     またはファイル内に timestamp があればその値）
     ※ INSERT 時刻ではなくセッション実施時刻を使う（時間減衰の正確性に関わる）
   - metadata: {"transcript_path": "...", "chunk_index": 0} を格納
```

**重要**: LLM は絶対に使わない。正規表現とルールベースのみ。判断に迷ったら多めに残す（後で検索時にフィルタできる）。

### フェーズ 5: 検索エンジン（search.py）

```
1. RRF 統合を実装する
   - storage.py の fts_search と vec_search を呼び出す
   - 各結果の順位から RRF スコアを算出: 1 / (k + rank)
   - 同一 ID のスコアを合算する
   - k=60（デフォルト）

2. 時間減衰を実装する
   - 各チャンクの created_at から経過日数を計算
   - decay = 0.5 ^ (days_old / 30)
   - RRF スコア × decay = 最終スコア

3. 統合検索関数を実装する
   - search(query, top_k=5) → [MemoryResult, ...]
   - 内部:
     a. クエリを FTS5 に渡してキーワード検索
     b. クエリに "query: " prefix を付けて Ruri v3 でベクトル化 → vec_search
     c. RRF + 時間減衰で統合
     d. 上位 top_k 件を返す
   - MemoryResult には chunk_text, score, created_at, session_id を含める
   - top_k のデフォルトは 5（CLI のデフォルトと一致させる）
```

### フェーズ 6: CLI（main.py）

CLI は標準ライブラリの argparse で実装する。click 等の追加依存は使わない（依存パッケージを 2 つに抑える設計原則）。

```
1. CLI コマンドを実装する（argparse の subparsers）
   - warmup: Ruri v3 モデルを事前ダウンロードする。初回セットアップ用
   - save: transcript パスを受け取り、チャンク化 → ベクトル化 → 保存
   - search: クエリと top_k（デフォルト 5）を受け取り、検索結果を標準出力に表示
   - stats: 総メモリ数、セッション数、DB サイズなどを表示
   - optimize: VACUUM、インデックス再構築

2. search コマンドの出力形式
   - CLAUDE.md の「search コマンドの出力フォーマット」に従う
   - ヘッダー行: [順位/総数] score=X.XXXX | YYYY-MM-DD | session=XXXXXXXX
   - 本文: チャンクテキスト
   - 区切り: ---
   - 0 件時: "No memories found for: <query>"

3. エラーハンドリング
   - 保存失敗時: warning をログに出力し、exit code 0 で終了する
   - セッション終了をブロックしてはいけない
   - timeout 30 秒以内に処理を完了すること（モデルは warmup 済み前提）
```

### フェーズ 7: 統合テストとドキュメント

```
1. tests/fixtures/sample_transcript.jsonl を作成する
   - 5〜10 ターン程度の短い会話サンプル
   - 実際の会話ログは使わない

2. 全テストが通ることを確認する
   - pytest tests/

3. README.md を作成する
   - セットアップ手順（uv sync → warmup → settings.json に Hook 追加）
   - カスタムスラッシュコマンド /memory の設定方法
   - CLI の使い方

4. Claude Code の settings.json に Hook を設定して E2E テストする
   - 環境変数名は Claude Code のドキュメントで確認する
```

---

## 判断基準（迷ったときの指針）

### Q: チャンクの粒度はどうするか？
A: 迷ったら小さくしすぎない。1 つの Q&A ペアが基本単位。分割しすぎると文脈が消える。足りない情報は検索時に top_k を増やせば補える。

### Q: 要約したほうがストレージ効率がよいのでは？
A: 要約しない。これは設計原則 2 に反する。LLM を使わないことが前提であり、要約過程で重要な文脈（なぜその判断をしたか等）が消えるリスクの方が大きい。

### Q: 埋め込みモデルを OpenAI API に変えたほうが精度が出るのでは？
A: 使わない。設計原則 1 に反する。全会話ログを外部に送信するプライバシーリスクと、API コストを考慮している。Ruri v3 は日本語特化で十分な性能がある。

### Q: MeCab を入れて形態素解析すべきか？
A: 入れない。trigram トークナイザで十分。追加の依存パッケージを最小限に抑えることが優先。固有名詞の検索は trigram で対応でき、意味的な検索はベクトル検索が補完する。

### Q: チャンクに重複がありそうだが、dedup すべきか？
A: 保存時の dedup は session_id の存在チェック（セッション単位のスキップ）のみ。DB 制約によるフォールバックは設けない。意味的な重複の判断は検索時に行う。

### Q: session_id の生成方法は？
A: transcript ファイル名の SHA-256 ハッシュの先頭 16 文字。同一ファイルの再保存は session_id の存在チェックで防がれる。

### Q: 検索結果を Claude Code にどうやって渡すのか？
A: search コマンドの標準出力経由。CLAUDE.md にカスタムスラッシュコマンド `/memory` を定義し、ユーザーが明示的に実行する方式を推奨。自動注入は Claude Code の Hook API の対応状況を確認してから検討する。

### Q: チャンクサイズの上限を「トークン数」で測るべきか？
A: トークナイザを追加しない。文字数カウント（約 2,000 文字）で代用する。日本語は 1 文字 ≈ 1〜2 トークンなので概ね 1,000〜2,000 トークン相当。tiktoken 等を追加すると依存パッケージが増えるため。

### Q: created_at は INSERT 時刻でよいか？
A: ダメ。セッション実施日時を明示的に指定する。理由は 3 つ: (1) 時間減衰がこの値に依存する、(2) 過去 transcript の一括インポート時に全て同一日時になる、(3) Hook 遅延時に不正確になる。

---

## テスト戦略

### 優先度高（必ず書く）

- **test_storage.py**: DB 初期化、insert（memories + memories_vec の ID 一致確認）、FTS 検索、ベクトル検索、二重保存防止（session_id 存在チェック）、created_at が指定値で格納されること
- **test_chunker.py**: 典型的な JSONL transcript が正しく Q&A ペアチャンクに分割されること、2,000 文字超のペアが分割されること、metadata に transcript_path と chunk_index が含まれること
- **test_search.py**: RRF スコア計算、時間減衰の数値、prefix 付き検索が正しいこと、top_k=5 がデフォルトであること

### 優先度中（余裕があれば）

- 大量データ（1,000+ チャンク）での検索速度が 100ms 以下であること
- 壊れた transcript（空ファイル、不正な JSONL）でクラッシュしないこと
- warmup コマンドがモデルをダウンロードできること（ネットワーク接続要）
- search コマンドの出力フォーマットが仕様通りであること

### テストデータ

テスト用の fixture として `tests/fixtures/sample_transcript.jsonl` を作成する。
5〜10 ターン程度の短い架空の会話。実際の会話ログは使わない。

---

## 実装時の注意事項

1. **コード量の目安は 1,759 行以下**。膨らみ始めたら機能を削ぎ落とす
2. **型ヒントと docstring を必ず書く**。docstring は日本語
3. **外部ネットワーク通信は一切しない**（warmup によるモデルの初回ダウンロードを除く）
4. **memory.db を git に commit しない**。.gitignore をフェーズ 1 で最初に作成する
5. **Hook のタイムアウト（30 秒）を意識する**。モデルは warmup 済み前提。保存処理が遅い場合はバッチサイズを調整する
6. **エラーでセッション終了をブロックしない**。全てのエラーは warning ログに留め、正常終了する
7. **Ruri v3 の prefix を忘れない**。保存時は `"passage: "` 、検索時は `"query: "` を必ず付与する
8. **Claude Code の Hook 環境変数名は実装時に確認する**。`$CLAUDE_TRANSCRIPT` は仮の名前
9. **CLI は argparse のみ**。click 等の追加パッケージは使わない（依存 2 つの原則）
10. **created_at は DEFAULT に頼らない**。セッション実施日時を明示的に INSERT する

---

## 実装完了の定義

以下の全てを満たしたとき、実装完了とする。

- [ ] `uv sync` のみでセットアップが完了する
- [ ] `warmup` コマンドで Ruri v3 モデルが事前ダウンロードされる
- [ ] settings.json に Hook を追加するだけでセッション終了時に自動保存される
- [ ] `search` コマンドでキーワード検索とベクトル検索のハイブリッド結果が返る
- [ ] search の出力フォーマットが CLAUDE.md の仕様通りである
- [ ] 検索時に `"query: "` prefix、保存時に `"passage: "` prefix が付与されている
- [ ] 検索のデフォルト top_k が 5 である
- [ ] 検索レスポンスが 100ms 以下である
- [ ] LLM を一切使っていない（トークン消費ゼロ）
- [ ] 外部サービスへの通信がない（SQLite 単一ファイルに閉じている）
- [ ] memories と memories_vec の ID が一致している
- [ ] 同一セッションの二重保存が session_id チェックで防止されている
- [ ] created_at にセッション実施日時が格納されている（INSERT 時刻ではない）
- [ ] metadata に transcript_path と chunk_index が含まれている
- [ ] チャンク分割が文字数ベース（約 2,000 文字）で行われている
- [ ] 主要モジュールのテストが全て通る
- [ ] memory.db が .gitignore に含まれている
- [ ] README.md にセットアップ手順とスラッシュコマンド設定方法が記載されている
