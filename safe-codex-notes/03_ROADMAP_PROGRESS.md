# ROADMAP_PROGRESS

## 運用方針

このファイルは、プロジェクトの達成度に応じて更新する。
各Phase完了時に、状態・達成度・実施内容・未完了事項を更新する。

## 現在の状態

| 項目 | 内容 |
|---|---|
| 現在の完了Phase | Phase 9-A |
| 現在の作業前状態 | Phase 9-A完了、Phase 9-B未開始 |
| 次のPhase | Phase 9-B: Codex custom model provider 検証 |
| current branch | `safe-codex/phase2-safe-profile` |
| base branch | `safe-codex-base` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| Phase 2 commit | `e69a4515 Add safe-codex profile wiring` |
| Phase 3-B commit | `efbdc811 Add safe-codex prompt caching for Codex websocket` |
| Phase 4 commit | `623324e0 Harden safe-codex logging` |
| latest commit | `Validate safe-codex OpenAI cache path on Windows` |
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
| 5 | 完了 | 100% | `headroom learn`抑制 |
| 6 | 完了 | 100% | Windows検証 |
| 7 | 完了 | 100% | ドキュメント整備 |
| 8 | 完了 | 100% | 総合レビュー |
| 9-A | 完了 | 100% | Codex Desktop Actions からsafe-codex proxyを起動・確認・停止する |

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

状態: 完了

達成度: 100%

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

状態: 完了

達成度: 100%

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

状態: 完了

達成度: 100%

### 実装commit

- `890c79b5 Guard safe-codex learn apply`

### 目的

`AGENTS.md` / `instructions.md` の自動変更を防ぐ。

### 実装内容

- `headroom learn` に `--allow-context-write` を追加。
- `HEADROOM_PROFILE=safe-codex` 時は `headroom learn --apply` を拒否。
- `HEADROOM_PROFILE=safe-codex` でも `--apply` なしのdry-runは許可。
- `HEADROOM_PROFILE=safe-codex` かつ `--apply --allow-context-write` の場合のみ書き込みを許可。
- safe-codex判定は `headroom/cli/_utils/safe_codex.py` に集約。
- `tests/test_cli_safe_codex.py` にenv復元fixtureを追加し、safe系テストが後続テストへ `HEADROOM_PROFILE` 等を漏らさないようにした。

### 変更ファイル

- `headroom/cli/_utils/safe_codex.py`
- `headroom/cli/learn.py`
- `tests/test_cli_learn.py`
- `tests/test_cli_safe_codex.py`

### 確認結果

- `python -m pytest tests/test_cli_learn.py`: 16 passed
- `python -m pytest tests/test_cli_safe_codex.py tests/test_cli_proxy_env.py`: 75 passed, 1 skipped
- related pytest: 118 passed, 1 skipped, 1 warning
- `ruff check headroom tests`: All checks passed
- `git diff --check`: CRLF warningのみ、whitespace errorなし

### 全体pytestの扱い

- command: `python -m pytest -q -x --tb=short`
- result: 1 failed, 90 passed, 88 skipped, 1 warning
- failure: `tests/test_adapter_hooks.py::TestStorageEntryPointLoading::test_sqlite_scheme`
- reason: Windows上で `sqlite:///{tmp_path}/test.db` が `\\C:\\...` 形式に解釈され、WinError 123で失敗
- judgement: Phase 5変更とは別領域の既存Windows path系失敗として扱う

### 完了条件

- safeでは `AGENTS.md` が無確認に変更されない。
- dry-run結果は表示できる。
- 明示許可時のみ書き込み可能。
## Phase 6: Windows検証

状態: 完了

達成度: 100%

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

状態: 完了

達成度: 100%

### 実装commit

- `Document safe-codex operation procedures`

### 目的

運用に必要な最小ドキュメントを整える。

### 作成・更新内容

- `safe-codex-notes/04_SAFE_CODEX_OPERATION.md` を新規作成。
- 導入前確認、safe-codex proxy起動、`headroom wrap codex --safe` 起動手順を整理。
- 安全設定の理由を表で整理。
- 危険オプションと使用禁止・慎重扱いの理由を整理。
- Windows検証手順を、CLI help確認、focused test、lint / format / diff check、OpenAI backend path検証に分けて整理。
- 切り戻し方法を、proxy停止、環境変数削除、未commit差分の戻し、基準branch復帰、commit済み変更の扱いに分けて整理。
- `safe-codex-notes/00_README.md` に `04_SAFE_CODEX_OPERATION.md` を正本Markdownとして追加。
- `safe-codex-notes/00_README.md` の現在状態をPhase 7完了、次PhaseをPhase 8へ更新。

### 変更ファイル

- `safe-codex-notes/00_README.md`
- `safe-codex-notes/03_ROADMAP_PROGRESS.md`
- `safe-codex-notes/04_SAFE_CODEX_OPERATION.md`

### 確認結果

- `git diff --check -- safe-codex-notes/00_README.md safe-codex-notes/03_ROADMAP_PROGRESS.md safe-codex-notes/04_SAFE_CODEX_OPERATION.md`: pass
- Markdownのみの変更のため、pytest / ruff は未実行。
- commit対象は正本Markdownのみ。
- `phase*_investigation/` は未追跡のままcommit対象外。

### Phase 7で判明した主な問題

- ChatGPTの外側コードブロック内にMarkdown本文用の三連バッククォートを含めると、貼り付けが途中で崩れる可能性がある。
- 長いMarkdownをPowerShell here-stringで流し込む場合、本文側のコードフェンスは三連バッククォートではなく `~~~` を使う方が安全。
- 新規未追跡ファイルは `git diff --stat` には出ないため、`git status --short` と `git diff --cached --stat` で確認する。

### 完了条件

- 自分が数週間後に見ても再開できる。
- 新規チャットで必要な前提を復元できる。
- Codexを使う場合でも作業範囲を誤らない。
## Phase 8: 総合レビュー

状態: 完了

達成度: 100%

### 目的

実運用へ入れてよいか総合判断する。

### 判定

`GO with conditions`

実運用へ入れてよい。ただし、以下の条件を守る。

- 個人ローカル運用に限定する。
- proxyはloopbackのみで起動する。
- `--log-messages` / `--codex-wire-debug` / `--codex-wire-debug-dir` は使わない。
- `wrap codex --safe --memory` は使わない。
- `headroom learn --apply` は原則使わず、必要時のみ `--allow-context-write` を明示する。
- `--openai-api-url` は検証用途中心とし、信頼できないendpointへAPI keyや内容を送らない。
- 医療情報、職場情報、患者情報、実データの本文ログは扱わない。
- `prompt_cache_retention` は通常 `in_memory` を使い、`24h` は慎重扱いにする。

### 確認観点

| 観点 | 判定 | 内容 |
|---|---|---|
| 安全性 | 条件付きOK | 本文ログ禁止、wire debug禁止、loopback制限、context自動書き換え抑制を確認 |
| 正確性 | OK | safe-codex明示時のみ新挙動を有効化する方針を確認 |
| Prompt Caching効率 | OK | `prompt_cache_key` / `prompt_cache_retention` / `cached_tokens` 経路を確認 |
| Windows安定性 | 条件付きOK | focused testsとruffは確認。mypyはnumpy stub / Python version解釈に起因する既知制約として扱う |
| 既存互換性 | OK | 通常profile互換性テストを確認 |
| 運用性 | OK | 5つの正本Markdown、検証手順、切り戻し手順を確認 |
| 未解決リスク | 許容 | 残リスクは運用条件として明文化 |

### Phase 8検証結果

- `python -m pytest tests/test_safe_codex_profile.py tests/test_cli_safe_codex.py tests/test_cli_learn.py`: 32 passed
- `python -m pytest tests/test_backends/test_litellm_cache_stats.py tests/test_proxy/test_openai_backend_path.py`: 13 passed, 1 warning
- `python -m pytest tests/test_cli_proxy_env.py tests/test_proxy_loopback_gating.py tests/test_provider_codex_runtime.py`: 86 passed, 1 skipped, 1 warning
- `ruff check headroom tests`: pass
- `ruff format --check headroom tests`: Phase 8で2ファイルのformat差分を検出し、修正対象化
- `mypy` scoped: numpy stub / Python version解釈に起因する失敗を確認。今回変更由来ではない既知制約として扱う
- `git diff --check`: pass

### Phase 8で判明した主な問題

- `phase8_command_results.txt` の集計では全コマンドが `ok=True` になったが、実ログ上は `ruff format --check` と `mypy` に失敗内容があった。
- PowerShellでnative commandの成否を集計する場合、`$?` だけでなく `$LASTEXITCODE` を即時保存して判定する必要がある。
- 正本Markdown内に `4つの正本Markdown` 表記が残っていた。
- `03_ROADMAP_PROGRESS.md` にPhase 3 / Phase 4の古い `未開始` ブロックが残っていた。
- `03_ROADMAP_PROGRESS.md` の `latest commit` がPhase 7 commitではなくPhase 6 commit相当のままだった。

### 残リスク

| ID | 残リスク | 扱い |
|---|---|---|
| R-009 | 実OpenAI/Codex長時間運用でのcache hit率は環境・会話内容に依存する | 実運用中にstatsで確認する |
| R-010 | `--prompt-cache-retention 24h` は情報残存期間が長くなる | 通常は `in_memory` を使う |
| R-011 | `--openai-api-url` を誤って信頼できないendpointへ向けるリスク | 検証用途中心とし、運用時は指定先を確認する |
| R-012 | full pytest / full mypy にはWindows既知問題が残る | focused test / related test / scoped mypyで切り分ける |
| R-013 | Phase調査スクリプトの成否集計がnative command失敗を拾えない場合がある | `$LASTEXITCODE` を即時記録する方式へ改善する |

### 切り戻し確認

- proxy停止は `Ctrl+C`。
- 一時環境変数は `04_SAFE_CODEX_OPERATION.md` の手順で削除する。
- 未commit差分は `git status --short --branch` と `git diff --stat` を確認してから戻す。
- commit済み変更を戻す場合、いきなり `git reset --hard` しない。

### 完了条件

- 実運用可否を判断済み。
- 残リスクを明文化済み。
- 切り戻し方法を確認済み。


## Phase 9-A: Codex Desktop Actions 運用補助

状態: 完了

達成度: 100%

### 目的

Codex Desktop Actions から `safe-codex` proxy を起動・確認・停止しやすくする。

### 対象

- `scripts/start-safe-codex-proxy.ps1`
- `scripts/check-safe-codex-status.ps1`
- `scripts/stop-safe-codex-env.ps1`
- `safe-codex-notes/05_CODEX_APP_OPERATION.md`
- `safe-codex-notes/00_README.md`
- `safe-codex-notes/03_ROADMAP_PROGRESS.md`

### 対象外

- `~/.codex/config.toml` の変更
- `~/.codex/.env` の変更
- API key の保存
- Codex Skill 化
- push

### 実装方針

- proxy起動は `127.0.0.1` 固定とする。
- `--host 0.0.0.0` は使わない。
- `--log-messages` / `--codex-wire-debug` / `--codex-wire-debug-dir` は使わない。
- `prompt_cache_retention` は `in_memory` とする。
- PowerShell 5.1で動く構成にする。
- `.venv\Scripts\Activate.ps1` があれば有効化する。
- `$env:PYTHONUTF8 = "1"` を設定する。
- API key、token、Authorization header、prompt本文、response本文をdocsやscript出力に残さない。

### 完了条件

- Actions用の起動・確認・停止scriptを作成する。
- Codex Desktop Actions向け手順を文書化する。
- focused test、ruff、format check、diff checkを実行する。
- local commitまで行う。
- pushしない。
## リスク管理表

| ID | リスク | 影響 | 対策 | 状態 |
|---|---|---:|---|---|
| R-001 | 圧縮でCodexが誤読する | 高 | 初期はlossless-first / Kompress無効 | Phase 2で一部対応 |
| R-002 | request/response本文がログに残る | 高 | `--log-messages` 禁止 | Phase 2でsafe時拒否を実装 |
| R-003 | Codex wire debugに機密情報が残る | 高 | safe profileで禁止 | Phase 2でsafe時拒否を実装 |
| R-004 | `AGENTS.md` が自動書き換えされる | 中 | safe時にwrap側の自動書き換えを抑制し、`headroom learn --apply` も明示許可制にする | Phase 5で対応済み |
| R-005 | Prompt Cachingが壊れる | 中 | `cache-first` / stable prefix | Phase 3-B / Phase 6で対応済み |
| R-006 | prompt_cache_keyに生パスが入る | 中 | hash化 | Phase 3-B / Phase 6で対応済み |
| R-007 | Windowsで起動しない | 高 | Phase 6で手動検証し、Phase 7で検証手順を文書化 | fake OpenAI経由のproxy検証と手順文書化は確認済み |
| R-008 | 通常Headroom挙動を壊す | 高 | safe明示時のみ新挙動 | Phase 2 / Phase 8テストで確認済み |
| R-009 | 実OpenAI/Codex長時間運用でのcache hit率は環境・会話内容に依存する | 中 | 実運用中にstatsで確認する | 残リスクとして許容 |
| R-010 | --prompt-cache-retention 24h による情報残存期間延長 | 中 | 通常は in_memory を使う | 残リスクとして許容 |
| R-011 | --openai-api-url を信頼できないendpointへ向ける | 高 | 検証用途中心、指定先確認を徹底 | 残リスクとして許容 |
| R-012 | full pytest / full mypy のWindows既知問題 | 低 | focused / related testで切り分ける | 残リスクとして許容 |
| R-013 | PowerShell native commandの失敗集計漏れ | 中 | $LASTEXITCODE を即時保存して判定する | Phase 8で検出、運用改善対象 |

## 更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | Phase 0完了内容を反映 |
| 2026-07-05 | 2 | Phase 1/2完了、`safe-codex` profile wiring実装commitを反映 |
| 2026-07-05 | 3 | Phase 3-B完了、Prompt Caching対応commitを反映 |
| 2026-07-05 | 4 | Phase 4完了、ログ安全化commitと作業方法を反映 |
| 2026-07-05 | 5 | Phase 5完了、headroom learn --apply 抑制commitを反映 |
| 2026-07-05 | 5 | 長い調査出力を画面表示せず phase*_investigation/ へファイル化する運用ルールを追記 |
| 2026-07-05 | 5 | Phase完了時に失敗・手戻り・改善策を運用ルールへ反映する共通ルールを追記 |
| 2026-07-05 | 6 | Windows検証完了、OpenAI backend path / prompt cache / cached_tokens / safe log / 通常profile互換性を確認 |
| 2026-07-05 | 7 | Phase 7完了、safe-codex運用手順、危険オプション、Windows検証手順、切り戻し方法を文書化 |
| 2026-07-05 | 8 | Phase 8完了、実運用は条件付き可、残リスクと切り戻し確認を反映 |
| 2026-07-08 | 9-A | Codex Desktop Actions向けのsafe-codex proxy起動・確認・停止手順を追加 |

## Phase 4以降の標準作業フロー

Phase 4以降は、Phase 3 / Phase 3-Bで安定した以下の流れを標準とする。

```text
1. 新規Phaseは新規チャットで開始
2. 5つの正本Markdownを参照
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

### まとめて実行可能なコード提示ルール

以後、ChatGPTがPowerShellコマンドを提示する場合は、可能な限りまとめて実行可能なコードとして提示する。

基本方針:

- 目的が1つの作業であれば、cd、環境変数、.venv 有効化、実行、確認までを1つのコードブロックにまとめる。
- $env:PYTHONUTF8 = "1" を原則含める。
- .venv\Scripts\Activate.ps1 が存在する場合は有効化する。
- $ErrorActionPreference = "Stop" を原則設定する。
- 作業前に必要なら phase*_investigation/ へバックアップする。
- 実行後に git diff --check、git status --short、必要に応じて git diff --stat を含める。
- commitまで行う指示の場合は、commit対象を明示し、phase*_investigation/ をaddしない。
- pushは禁止。


### Phase完了時の失敗・改善反映ルール

各Phase完了時は、実装内容・テスト結果だけでなく、そのPhase中に発生した失敗、手戻り、想定外の挙動、改善策も運用ルールへ反映する。

記録対象:

- 貼り付けたscriptが途中で崩れた作業。
- PowerShell / Python / encoding 由来の文字化け。
- PowerShell 5.1 のBOM付きUTF-8保存による不要差分。
- .NET の File API へ相対パスを渡したことで、PowerShellの表示上のカレントディレクトリと異なる場所を参照した作業。
- `Get-Content` 等で長い出力を画面に出して扱いにくくなった作業。
- test順序依存、環境変数漏れ、fixture不足などのテスト運用問題。
- 既知失敗と今回変更由来の失敗を切り分けた結果。
- 次回以降の手順を変えるべき作業。
- 運用ルール、コマンド提示方法、検証手順に反映すべき改善点。

反映方法:

- Phase完了時に `03_ROADMAP_PROGRESS.md` へ、そのPhaseで得た失敗と改善策を追記する。
- 恒常的な運用変更は `00_README.md` にも反映する。
- 設計判断へ影響する内容は `02_DESIGN_DECISIONS.md` にも反映する。
- 出力が長くなる検証結果や調査結果は画面表示せず、`phase*_investigation/` 配下に保存する。
- `phase*_investigation/` はcommitしない。

Phase 5で得た改善策:

- PowerShell 5.1 から Python stdin / here-string へ日本語入りMarkdownを渡す更新scriptは避ける。
- PowerShell 5.1 の Set-Content -Encoding UTF8 はBOM付きUTF-8になるため、正本Markdown更新では .NET の WriteAllText と UTF8Encoding(false) を使う。
- .NET の File API を使う場合は、Resolve-Path / Join-Path で絶対パス化してから渡す。
- 日本語Markdown更新は、PowerShell側で UTF-8 入出力を明示して `Get-Content -Raw -Encoding UTF8` / `Set-Content -Encoding UTF8` で処理する。
- Markdown内に三連バッククォートを含む長い置換scriptは、貼り付け崩れや構文崩れの原因になるため避ける。
- 長い調査結果は画面表示せず、`phase*_investigation/` 配下に保存して、必要時にファイルをアップロードする。
- テストで `os.environ` を直接変更する処理がある場合、`yield` fixture などで終了時に明示復元する。
- 関連テストで失敗した場合は、単独実行と順序実行を分け、今回変更由来か既存の順序依存かを切り分ける。
- 既知失敗は、失敗名・原因・今回変更との関連有無を正本Markdownに記録する。

### 長い出力のファイル化ルール

調査コマンド、grep、diff、ログ抽出、コード断片抽出など、結果出力が長くなる可能性がある作業では、原則として画面に直接全文を出さない。

運用:

- `phase*_investigation/` 配下に結果ファイルを作成する。
- 画面には作成ファイルパス、件数、短い要約のみ表示する。
- 必要に応じて、そのファイルをChatGPTへアップロードする。
- `Get-Content` で全文表示する指示は原則避ける。
- 貼り付けが必要な場合も、必要範囲だけを抽出する。
- commit対象には `phase*_investigation/` を含めない。

分割する条件:

- ChatGPT画面上のコピー可能範囲を超えそうな場合。
- here-stringや長いPythonスクリプトを含み、途中で切れるリスクがある場合。
- 失敗時の影響範囲を小さくしたい場合。
- 調査、patch、test、commitを明確に分けた方が安全な場合。

分割する場合の標準:

1. 調査・抽出
2. 小さいpatch
3. focused test / ruff / diff check
4. commit

注意:

- 長い自動置換より、短いStepに分けた安全な実行を優先する。
- ただし、短く安全に収まる作業は、最初からまとめて実行可能なコードとして提示する。

<!-- PHASE6-WINDOWS-VALIDATION-START -->
## Phase 6 Windows検証 結果

状態: 完了

達成度: 100%

### 実装commit

- `Validate safe-codex OpenAI cache path on Windows`
- 最新hashは `git log -1 --oneline` で確認する。

### 実施結果

- Windows / PowerShell 5.1 / Python 3.12.13 環境で、safe-codex profile の実機検証を実施。
- fake OpenAI endpoint 経由で `headroom proxy --profile safe-codex --backend openai --openai-api-url ...` の到達を確認。
- `prompt_cache_key=codex:<opaque hash>` と `prompt_cache_retention=in-memory` が上流request bodyへ送信されることを確認。
- `usage.prompt_tokens_details.cached_tokens` を `cache_read_input_tokens` / `prompt_tokens_details.cached_tokens` として取り込めることを確認。
- safe proxy stdout / stderr / health / stats に prompt本文、Authorization、dummy API key が残らないことを確認。
- 通常profile互換性を壊さないため、`api_base` は対応backendにのみ渡す実装に修正。

### 変更ファイル

- `headroom/backends/litellm.py`
- `headroom/providers/registry.py`
- `headroom/proxy/handlers/openai.py`
- `tests/test_backends/test_litellm_cache_stats.py`
- `tests/test_proxy/test_openai_backend_path.py`

### 最終確認

- scoped tests: `151 passed, 1 skipped`
- focused compatibility tests: `92 passed, 1 skipped`
- ruff: `headroom tests` および変更ファイルで `All checks passed!`
- mypy: 変更source 3件で `Success`
- `git diff --check`: CRLF warningのみ
- 既知対象外:
  - Windows SQLite file lock / sqlite path issue
  - full mypy の `headroom/release_version.py` 既存 `tomllib` no-redef
  - `phase*_investigation/` 配下の一時scriptに対するruff

### Phase 6で判明した主な問題

- `python - <<'PY'` は PowerShell 5.1 非対応で、ParserError と誤実行を誘発する。
- `headroom proxy --no-open` は実CLIに存在せず、help照合不足だった。
- `--openai-api-url` は表示上のrouteには反映されるが、当初 LiteLLM 実送信へ `api_base` が渡っていなかった。
- `prompt_cache_key` / `prompt_cache_retention` は通常kwargsではLiteLLM経由のHTTP bodyへ出ず、`extra_body` 経由にする必要があった。
- backend factoryで常に `api_base` を渡すと、テスト用 injected backend 互換性を壊すため、signature確認が必要だった。
- `git diff --check` は PowerShell `$ErrorActionPreference=Stop` と組み合わせるとCRLF warningでも停止し得る。
- `ruff check .` は `phase*_investigation/` の一時scriptを拾うため、tracked source/testに限定する。
- `ruff` に全tracked pyを展開するとWindows引数長制限に当たるため、`ruff check headroom tests` を使う。
- Markdown本文にバッククォートを含む場合、PowerShell double-quoted here-string では `` `a `` / `` `r `` / `` `t `` が制御文字化するため、single-quoted here-stringを使う。
<!-- PHASE6-WINDOWS-VALIDATION-END -->
