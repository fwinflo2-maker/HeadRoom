# DESIGN_DECISIONS

## 基本設計

新しい圧縮器を作るのではなく、既存Headroomに `safe-codex` profileを追加する。

## 初期目標

```text
safe-codex profile
+ cache-first
+ lossless-first
+ no sensitive logs
+ loopback only
+ AGENTS.md auto-write suppression
+ prompt_cache_key support
+ cached_tokens metrics
```

## 設計原則

- 最大圧縮より、安全性・再現性・Windows/Codex運用での実用性を優先する。
- `safe-codex` 明示時のみ新挙動を有効化する。
- 通常の `headroom proxy` / `headroom wrap codex` の既存挙動は壊さない。
- 大規模リファクタリングは避ける。
- request / response本文を保存しない。
- API key、GitHub token、個人情報、職場情報、医療情報をログやcache keyに含めない。

## Phase 2で採用した構成

### 実装場所

`safe-codex` profile相当の判定・既定値・危険option検証は以下に集約する。

```text
headroom/cli/_utils/safe_codex.py
```

### 採用理由

- 既存に `headroom/cli/_utils/` が存在する。
- Phase 2ではCLI入口の最小変更で足りる。
- `headroom/profiles/` という新しいprofile基盤を作ると過剰設計になる。
- `proxy.py` と `wrap.py` の重複実装を避けられる。

### 不採用

```text
headroom/profiles/__init__.py
headroom/profiles/safe_codex.py
```

理由:

- 現時点では既存 `headroom/profiles/` がない。
- CLI以外からprofile参照する要件がまだない。
- Phase 2の目的に対して新しいprofile基盤は大きすぎる。

## safe-codexの既定値

Phase 2で実装済み:

```text
HEADROOM_PROFILE=safe-codex
HEADROOM_MODE=cache
HEADROOM_LOSSLESS=1
HEADROOM_DISABLE_KOMPRESS=1
HEADROOM_LOG_MESSAGES=0
HEADROOM_NO_CCR_INJECT_TOOL=1
HEADROOM_NO_CCR_MARKER=1
HEADROOM_CODE_AWARE_ENABLED=0
HEADROOM_OUTPUT_SHAPER=0
HEADROOM_HOST=127.0.0.1
```

Phase 3へ分離:

```text
HEADROOM_CACHE_FIRST=1
HEADROOM_STABLE_PREFIX=1
```

理由:

- Phase 2確認時点では実効的な参照箇所が未確定。
- Prompt Caching対応と一緒に設計・実装する方が安全。

## CLI

Phase 2で実装済み:

```powershell
headroom proxy --profile safe-codex --port 8787
headroom wrap codex --safe
```

Prompt Caching利用時の想定CLIはPhase 3で扱う。

```powershell
headroom wrap codex --safe --prompt-cache-key auto --prompt-cache-retention in_memory
```

## safe-codexで拒否するもの

| 対象 | 扱い | 理由 |
|---|---|---|
| `--host 0.0.0.0` | 拒否 | ローカルプロキシ公開リスク |
| 非loopback host | 拒否 | 外部公開リスク |
| `HEADROOM_HOST=0.0.0.0` | 拒否 | 外部公開リスク |
| `--log-messages` | 拒否 | request/response本文が残る |
| `HEADROOM_LOG_MESSAGES=1` | 拒否 | request/response本文が残る |
| `--codex-wire-debug` | 拒否 | Codex通信内容が残る |
| `--codex-wire-debug-dir` | 拒否 | Codex通信内容が残る |
| `HEADROOM_CODEX_WIRE_DEBUG=1` | 拒否 | Codex通信内容が残る |
| `wrap codex --safe --memory` | 拒否 | 永続memory/context file書き込みリスク |
| `wrap codex --safe --codex-wire-debug` | 拒否 | Codex通信内容が残る |
| `headroom learn --apply` with safe-codex | 拒否 | `AGENTS.md` / `instructions.md` 等の無確認なcontext書き込みリスク |
| `headroom learn --apply --allow-context-write` with safe-codex | 許可 | 明示許可がある場合のみcontext書き込みを認める |

## wrap codex --safe の方針

safe時は以下を抑制する。

```text
RTK AGENTS注入
MCP自動登録
tokensave登録
Serena登録
memory guidance注入
coding compressor setup
```

ただし、Codex通信をproxyへ向けるprovider configと `OPENAI_BASE_URL` 設定は維持する。

## Prompt Caching方針

Prompt CachingはPhase 3へ分離する。

### Phase 3で確認すること

- `prompt_cache_key` をOpenAI request bodyへ入れる箇所。
- `prompt_cache_retention` を扱う箇所。
- `cached_tokens` をusage metricsへ反映する箇所。
- auto keyに生パス・ユーザー名・token・API keyを含めない方法。
- safe-codex時のみ有効化する分岐。
- 既存OpenAI/Codex routing testへの追加位置。

## metrics方針

記録してよいもの:

```text
timestamp
request_id
model
prompt_tokens
cached_tokens
tokens_before
tokens_after
latency_ms
cache_hit_ratio
safe_profile_enabled
prompt_cache_key_enabled
prompt_cache_retention
```

記録しないもの:

```text
prompt本文
tool output本文
response本文
diff全文
error log全文
API key
token
絶対パス
```

## redact対象

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
GITHUB_TOKEN
GH_TOKEN
sk-
ghp_
github_pat_
Authorization: Bearer ...
```

## テスト方針

Phase 2で実行済み:

```powershell
python -m pytest tests/test_safe_codex_profile.py tests/test_cli_safe_codex.py
python -m pytest tests/test_cli_proxy_env.py tests/test_proxy_loopback_gating.py tests/test_provider_codex_runtime.py tests/test_cli_learn.py
ruff check headroom tests
ruff format --check headroom tests
mypy headroom
```

確認結果:

```text
新規pytest: 15 passed
既存pytest: 99 passed, 1 skipped, 1 warning
ruff check: pass
ruff format --check: pass
mypy: numpy stub起因で失敗、Phase 2対象外
```

## 採用済み判断

| ID | 判断 | 理由 |
|---|---|---|
| D-001 | 新規圧縮器ではなく既存Headroomにprofile追加 | 影響範囲が小さく、既存CLIとproxyを活かせる |
| D-002 | 初期はlossless-first | Codex誤読リスクを下げる |
| D-003 | safe-codexはcache-first | Prompt Cachingを壊さないため |
| D-004 | 本文ログを禁止 | API key、個人情報、職場情報の残存リスクを下げる |
| D-005 | `AGENTS.md` 自動書き換えを抑制 | Codex運用ルールの正本を壊さないため |
| D-006 | Phase 2では `headroom/cli/_utils/safe_codex.py` に集約 | 最小変更でCLI実装に閉じるため |
| D-007 | Prompt CachingはPhase 3へ分離 | request body変換・metrics集計まで影響が広がるため |
| D-008 | `learn --apply` 抑制はPhase 5へ分離 | Phase 2範囲を超えるため |

## 未決定事項

| ID | 未決定事項 | 判断タイミング |
|---|---|---|
| U-003 | `prompt_cache_key` 注入箇所 | Phase 3 |
| U-004 | `cached_tokens` 集計箇所 | Phase 3 |
| U-005 | `learn --apply` 抑制の実装方法 | Phase 5 |
| U-006 | Windows実運用でのproxy/Codex動作 | Phase 6 |

## 設計更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | 初期設計方針を作成 |
| 2026-07-05 | 2 | Phase 2実装方針と実装場所を反映 |
| 2026-07-05 | 5 | Phase 5で headroom learn --apply のsafe-codex明示許可制を反映 |

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

<!-- PHASE6-DESIGN-DECISIONS-START -->
## Phase 6 Windows検証で確定した設計判断

- OpenAI上流URLの切替は、proxy表示だけでなく LiteLLM backend の実送信kwargsへ `api_base` として渡す。
- LiteLLM OpenAI-format送信では、OpenAI互換raw request field の `prompt_cache_key` / `prompt_cache_retention` を `extra_body` に入れて渡す。
- safe-codex profile の prompt cache option は、client指定値を上書きしない。
- `prompt_cache_key=auto` はローカル絶対パスやユーザー名を露出しない opaque hash とする。
- backend factoryは、injected backend / test fake backendの互換性を保つため、`api_base` を受け取れるbackendにのみ渡す。
- safe-codexでは本文ログ、API key、Authorization、prompt本文をstdout/stderr/health/statsへ残さない。
- Windows検証では `phase*_investigation/` をruff対象外とし、最終lintは `ruff check headroom tests` と変更ファイルruffで確認する。
<!-- PHASE6-DESIGN-DECISIONS-END -->
