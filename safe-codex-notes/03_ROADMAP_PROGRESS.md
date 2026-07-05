# ROADMAP_PROGRESS

## 運用方針

このファイルは、プロジェクトの達成度に応じて更新する。
各Phase完了時に、状態・達成度・実施内容・未完了事項を更新する。

## 現在の状態

| 項目 | 内容 |
|---|---|
| 現在の完了Phase | Phase 4 |
| 現在の作業前状態 | Phase 4完了、Phase 5未開始 |
| 次のPhase | Phase 5: headroom learn抑制 |
| current branch | `safe-codex/phase2-safe-profile` |
| base branch | `safe-codex-base` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| Phase 2 commit | `e69a4515 Add safe-codex profile wiring` |
| Phase 3-B commit | `efbdc811 Add safe-codex prompt caching for Codex websocket` |
| Phase 4 commit | `623324e0 Harden safe-codex logging` |
| latest commit | `623324e0 Harden safe-codex logging` |
| push | 未実行 |
| worktree | ソース差分なし、`phase*_investigation/` のみ未追跡想定 |

## Phase一覧

| Phase | 状態 | 達成度 | 目的 |
|---:|---|---:|---|
| 0 | 完了 | 100% | upstream固定・作業環境確認 |
| 1 | 完了 | 100% | `safe-codex`設計・変更対象確定 |
| 2 | 完了 | 100% | `safe-codex` profileとCLI追加 |
| 3 | 完了 | 100% | Prompt Caching対応 |
| 4 | 完了 | 100% | ログ安全化 |
| 5 | 未開始 | 0% | `headroom learn`抑制 |
| 6 | 未開始 | 0% | Windows検証 |
| 7 | 未開始 | 0% | ドキュメント整備 |
| 8 | 未開始 | 0% | 総合レビュー |

## Phase 0: upstream固定・環境確認

状態: 完了

達成度: 100%

### 目的

改変対象となるHeadroomの基準tag / commitを固定し、Windowsローカルで開発に必要な環境を揃える。

### 完了内容

- `C:\dev\headroom-safe-codex` にrepo clone済み。
- `v0.30.0` を基準に固定済み。
- `safe-codex-base` branch作成済み。
- Python 3.12.13仮想環境作成済み。
- Git / uv / Rust / cargo / maturin / Codex CLI / MSVC確認済み。
- Execution Policyを `CurrentUser RemoteSigned` に変更済み。

### 成果物

- base branch: `safe-codex-base`
- base tag: `v0.30.0`
- base commit: `728b3308`
- Python: `3.12.13`
- local repo: `C:\dev\headroom-safe-codex`

### 残課題

なし。

## Phase 1: safe-codex詳細設計

状態: 完了

達成度: 100%

### 目的

実装前に既存構成を確認し、`safe-codex` profileを最小変更で追加する設計を確定する。

### 完了内容

- 既存構成を確認。
- `headroom/cli/proxy.py`、`headroom/cli/wrap.py`、`headroom/providers/codex/runtime.py`、`tests/`、`pyproject.toml` を確認。
- 新しい圧縮器は作らず、既存Headroomに `safe-codex` profile相当を追加する方針を確定。
- `headroom/profiles/` はPhase 2では作らず、CLI utilsへ集約する方針を確定。
- Prompt CachingはPhase 3へ分離する方針を確定。
- `headroom learn --apply` 抑制はPhase 5へ分離する方針を確定。

### 成果物

- Phase 2実装対象リスト
- 追加ファイル案
- 変更ファイル案
- テスト方針
- 未決定事項の整理

### 残課題

なし。Prompt CachingはPhase 3で扱う。

## Phase 2: safe-codex最小実装

状態: 完了

達成度: 100%

### 実装commit

- `e69a4515 Add safe-codex profile wiring`

### 実装内容

- `headroom/cli/_utils/safe_codex.py` を追加。
- `headroom proxy --profile safe-codex` を追加。
- `headroom wrap codex --safe` を追加。
- safe時のloopback制限を追加。
- safe時の危険ログoption拒否を追加。
- safe時のCodex/MCP/context自動書き換え抑制を追加。
- 新規テストを追加。

### 変更ファイル

```text
headroom/cli/_utils/safe_codex.py
headroom/cli/proxy.py
headroom/cli/wrap.py
tests/test_cli_safe_codex.py
tests/test_safe_codex_profile.py
```

### 確認結果

```text
python -m pytest tests/test_safe_codex_profile.py tests/test_cli_safe_codex.py
=> 15 passed

python -m pytest tests/test_cli_proxy_env.py tests/test_proxy_loopback_gating.py tests/test_provider_codex_runtime.py tests/test_cli_learn.py
=> 99 passed, 1 skipped, 1 warning

ruff check headroom tests
=> All checks passed

ruff format --check headroom tests
=> 936 files already formatted

mypy headroom
=> .venv\Lib\site-packages\numpy\__init__.pyi の syntax error で失敗
=> Phase 2変更由来ではなく、依存stub / 環境起因として扱う
```

### 残課題

- Prompt Caching対応はPhase 3へ分離。
- ログ安全化の追加強化はPhase 4へ分離。
- `headroom learn --apply` 抑制はPhase 5へ分離。
- Windows実運用検証はPhase 6へ分離。

## Phase 3: Prompt Caching対応

状態: 未開始

達成度: 0%

### 目的

OpenAI Prompt Cachingを壊さず、cache hit率を上げる。

### 実装対象

- `prompt_cache_key`
- `prompt_cache_retention`
- `cached_tokens` metrics

### 完了条件

- auto keyに絶対パスやユーザー名が含まれない。
- request bodyに `prompt_cache_key` が入る。
- request bodyに `prompt_cache_retention` が入る。
- `cached_tokens` が取れる場合はstatsに反映される。
- `cached_tokens` がない場合も落ちない。

## Phase 4: ログ安全化

状態: 未開始

達成度: 0%

### 目的

機密情報がログやdebug snapshotに残らないようにする。

### 実装対象

- redact関数追加
- 数値メトリクス中心のログ設計
- Phase 2で拒否済みの `--log-messages` / `--codex-wire-debug` の補強

### 完了条件

- safeでは本文ログが保存されない。
- API key風文字列がログに残らない。
- ログは数値メトリクス中心。

## Phase 5: headroom learn抑制

状態: 未開始

達成度: 0%

### 目的

`AGENTS.md` / `instructions.md` の自動変更を防ぐ。

### 方針

- dry-runは許可。
- `--apply` はsafe profileでは拒否。
- `--allow-context-write` 明示時のみ許可。

### 完了条件

- safeでは `AGENTS.md` が無確認に変更されない。
- dry-run結果は表示できる。
- 明示許可時のみ書き込み可能。

## Phase 6: Windows検証

状態: 未開始

達成度: 0%

### 目的

Windowsローカル環境で実用可能か検証する。

### 確認対象

- Python test
- ruff
- mypy
- proxy起動
- Codex連携
- Prompt Caching metrics
- ログ安全性
- 通常profile互換性

### 完了条件

- Windowsで起動する。
- Codexがproxy経由で動く。
- `cached_tokens` が見える。
- 本文ログが残らない。
- 通常のCodex作業が破綻しない。

## Phase 7: ドキュメント整備

状態: 未開始

達成度: 0%

### 目的

運用に必要な最小ドキュメントを整える。

### 作成対象

- 導入手順
- 安全設定の理由
- 危険オプション
- Windows検証手順
- 切り戻し方法

### 完了条件

- 自分が数週間後に見ても再開できる。
- 新規チャットで必要な前提を復元できる。
- Codexを使う場合でも作業範囲を誤らない。

## Phase 8: 総合レビュー

状態: 未開始

達成度: 0%

### 目的

実運用へ入れてよいか総合判断する。

### 確認観点

- 安全性
- 正確性
- Prompt Caching効率
- Windows安定性
- 既存互換性
- 運用性
- 未解決リスク

### 完了条件

- 実運用可否を判断済み。
- 残リスクを明文化済み。
- 切り戻し方法を確認済み。

## リスク管理表

| ID | リスク | 影響 | 対策 | 状態 |
|---|---|---:|---|---|
| R-001 | 圧縮でCodexが誤読する | 高 | 初期はlossless-first / Kompress無効 | Phase 2で一部対応 |
| R-002 | request/response本文がログに残る | 高 | `--log-messages` 禁止 | Phase 2でsafe時拒否を実装 |
| R-003 | Codex wire debugに機密情報が残る | 高 | safe profileで禁止 | Phase 2でsafe時拒否を実装 |
| R-004 | `AGENTS.md` が自動書き換えされる | 中 | safe時にwrap側の自動書き換えを抑制 | Phase 2で一部対応、Phase 5で追加対応 |
| R-005 | Prompt Cachingが壊れる | 中 | `cache-first` / stable prefix | Phase 3で対応予定 |
| R-006 | prompt_cache_keyに生パスが入る | 中 | hash化 | Phase 3で対応予定 |
| R-007 | Windowsで起動しない | 高 | Phase 6で手動検証 | 未確認 |
| R-008 | 通常Headroom挙動を壊す | 高 | safe明示時のみ新挙動 | Phase 2テストで確認済み |

## 更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | Phase 0完了内容を反映 |
| 2026-07-05 | 2 | Phase 1/2完了、`safe-codex` profile wiring実装commitを反映 |
| 2026-07-05 | 3 | Phase 3-B完了、Prompt Caching対応commitを反映 |
| 2026-07-05 | 4 | Phase 4完了、ログ安全化commitと作業方法を反映 |

## Phase 4以降の標準作業フロー

Phase 4以降は、Phase 3 / Phase 3-Bで安定した以下の流れを標準とする。

```text
1. 新規Phaseは新規チャットで開始
2. 4つの正本Markdownを参照
3. 現在状態を開始プロンプトに明記
4. まず調査
5. 必要箇所だけ抽出
6. 小分けpatch
7. focused test
8. ruff
9. final test
10. local commit
11. pushしない
```

運用ルール:

- 巨大patch貼り付けは避ける。
- PowerShellはなるべくコピーしやすくまとめる。
- 長くなる場合はStep単位で分割する。
- .venv / Python 3.12.13 を優先する。
- $env:PYTHONUTF8 = "1" を原則設定する。
- phase*_investigation/ は原則commitしない。
- pushは禁止し、local commitまでに留める。
- 通常profileを壊さず、safe-codex 明示時のみ変更する。
- CodexではなくChatGPT主導で、設計・差分作成・レビュー・テスト方針を進める。



## Phase 3/4 完了追記

### Phase 3-B: Prompt Caching対応

状態: 完了

実装commit:

- `efbdc811 Add safe-codex prompt caching for Codex websocket`

実装内容:

- `safe-codex` 用 `prompt_cache_key` / `prompt_cache_retention` optionを追加。
- HTTP `/v1/chat/completions` に対応。
- HTTP `/v1/responses` に対応。
- Codex WebSocket `/v1/responses response.create` に対応。
- `auto` keyは `codex:<hash>` 形式とし、絶対パスを送らない。
- client指定済み値は上書きしない。
- `cached_tokens` がない場合も落ちない。

確認結果:

- pytest: 40 passed
- ruff: All checks passed

### Phase 4: ログ安全化

状態: 完了

実装commit:

- `623324e0 Harden safe-codex logging`

実装内容:

- `ProxyConfig.safe_mode` を追加。
- `cli proxy --profile safe-codex` から `safe_mode=True` を伝播。
- `RequestLogger` に `safe_mode` を追加。
- safe時は `log_full_messages=True` が渡されても本文ログを強制無効化。
- safe時は `request_messages` / `compressed_messages` / `response_content` をrecent logにもJSONLにも保持しない。
- safe時は `get_recent_with_messages()` でも本文系フィールドを返さない。
- safe時は `HEADROOM_DEBUG_DUMP=full` でもdebug dumpをoffにする。
- safe log metadata内の token / API-key風文字列をredact。
- 通常profileの `log_full_messages=True` 挙動は維持。

確認結果:

- focused pytest: 40 passed
- related pytest: 101 passed, 1 warning
- ruff check headroom tests: All checks passed
- git diff --check: pass

全体pytestの扱い:

- command: python -m pytest -q -x --tb=short
- result: 1 failed, 90 passed, 88 skipped, 1 warning
- failure: tests/test_adapter_hooks.py::TestStorageEntryPointLoading::test_sqlite_scheme
- reason: Windows上で sqlite:///{tmp_path}/test.db が \C:\... 形式に解釈され、WinError 123で失敗
- judgement: Phase 4ログ安全化変更とは別領域の既存Windows path系失敗として扱う

### 今回の作業方法

Phase 3完了時の運用ルール反映と同じく、以下の流れで進めた。

1. 正本Markdownと開始時状態を確認。
2. 広めにログ面を調査。
3. 候補が広すぎたため、高リスク箇所だけ再抽出。
4. 実際に本文を保持・保存し得る経路に絞った。
5. `RequestLogger` / debug dump / `ProxyConfig` / CLI伝播に限定して小分けpatch。
6. focused testを実行。
7. 関連テストへ拡大。
8. ruffを実行。
9. `git diff --check` を実行。
10. 全体pytestの既知失敗を切り分け。
11. 実装差分だけlocal commit。
12. Phase完了後、正本Markdownに作業方法と結果を反映。

### Phase完了時の正本更新ルール

- 実装commit完了後、すぐに `safe-codex-notes/03_ROADMAP_PROGRESS.md` を更新する。
- 更新前に `safe-codex-notes/03_ROADMAP_PROGRESS.md` を `phase*_investigation/` へバックアップする。
- 正本Markdownは全置換しない。既存構成を確認し、必要箇所だけ追記・更新する。
- 長いスクリプトはコピー可能範囲で途切れる可能性があるため、短いStepに分割する。
- 反映内容には、Phase状態、実装commit、変更概要、作業方法、test / ruff / `git diff --check` 結果を含める。
- 全体testで既知失敗がある場合は、失敗名・原因・今回変更との関連有無を明記する。
- Markdown更新のみの場合、pytestは原則不要。必要に応じて `git diff --check` のみ実施する。
- commit対象は正本Markdownのみとし、`phase*_investigation/` はcommitしない。
