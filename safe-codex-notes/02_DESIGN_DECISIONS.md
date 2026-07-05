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
- まずは安全な既定値セットを追加し、圧縮強度は後から段階的に調整する。
- request / response本文を保存しない。
- API key、GitHub token、個人情報、職場情報、医療情報をログやcache keyに含めない。

## safe-codexの既定値案

```text
HEADROOM_PROFILE=safe-codex
HEADROOM_MODE=cache
HEADROOM_CACHE_FIRST=1
HEADROOM_STABLE_PREFIX=1
HEADROOM_LOSSLESS=1
HEADROOM_DISABLE_KOMPRESS=1
HEADROOM_LOG_MESSAGES=0
HEADROOM_NO_CCR_INJECT_TOOL=1
HEADROOM_NO_CCR_MARKER=1
HEADROOM_CODE_AWARE_ENABLED=0
HEADROOM_OUTPUT_SHAPER=0
HEADROOM_HOST=127.0.0.1
```

## 想定CLI

```powershell
headroom proxy --profile safe-codex --port 8787 --no-open
headroom wrap codex --safe
```

Prompt Caching利用時:

```powershell
headroom wrap codex --safe --prompt-cache-key auto --prompt-cache-retention in_memory
```

節約優先時:

```powershell
headroom wrap codex --safe --prompt-cache-key auto --prompt-cache-retention 24h
```

## 変更候補ファイル

```text
headroom/profiles/__init__.py
headroom/profiles/safe_codex.py
headroom/cli/proxy.py
headroom/cli/wrap.py
tests/test_safe_codex_profile.py
```

Prompt Caching対応時は、OpenAI request bodyの変換箇所とusage集計箇所を追加で確認する。

## safe-codexで禁止または明示許可にするもの

| 項目 | safe-codexでの扱い | 理由 |
|---|---|---|
| `--host 0.0.0.0` | 原則拒否 | ローカルプロキシ公開リスク |
| `--log-messages` | 拒否 | request/response本文が残る |
| `--codex-wire-debug` | 拒否 | Codex通信内容が残る |
| `headroom learn --apply` | 拒否または明示許可制 | `AGENTS.md` 自動変更リスク |
| `prompt_cache_retention 24h` | 明示指定時のみ | 機密性への配慮 |

## Prompt Caching方針

`safe-codex` は `cache-first` を基本にする。

方針:

- 既存の `--mode cache` を活用する。
- 固定prefixを安定させる。
- 変動ログ・diff・今回指示は末尾に寄せる。
- prior turnsを無理に毎回再圧縮しない。
- `cached_tokens` を計測して効果を確認する。

## 推奨prompt構造

```text
[固定prefix]
- system/developer instructions
- AGENTS.md
- プロジェクト共通ルール
- テスト方針
- 禁止事項
- 出力フォーマット

[準固定prefix]
- repository summary
- directory map
- known commands
- dependency summary

[変動tail]
- 今回の依頼
- git diff
- test log
- error log
- review comment
- Codexへの今回指示
```

## prompt_cache_key方針

指定案:

```text
--prompt-cache-key auto
--prompt-cache-key <key>
--no-prompt-cache-key
```

auto key:

- repo rootまたはworking directoryをもとにhash化する。
- 生の絶対パスは送らない。
- ユーザー名、職場名、token、API keyを含めない。
- 例: `codex:<repo-root-hash>`

## prompt_cache_retention方針

指定案:

```text
--prompt-cache-retention default
--prompt-cache-retention in_memory
--prompt-cache-retention 24h
```

初期方針:

- safe-codexの既定は `default` または `in_memory`。
- `24h` は明示指定時のみ。
- 医療情報・職場情報を含む可能性がある場合は `24h` を避ける。

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

最小テスト:

```powershell
python -m pytest tests/test_safe_codex_profile.py
python -m pytest tests/test_cli_wrap.py tests/test_cli_proxy.py
ruff check headroom tests
ruff format --check headroom tests
mypy headroom
```

確認すること:

- `safe-codex` 明示時のみ新挙動になる。
- 通常profileの既存挙動を壊していない。
- `safe-codex + --host 0.0.0.0` は失敗する。
- `safe-codex + --log-messages` は失敗する。
- `safe-codex + --codex-wire-debug` は失敗する。
- `wrap codex --safe` が `OPENAI_BASE_URL=http://127.0.0.1:<port>/v1` を設定する。
- `prompt_cache_key auto` に生パス・ユーザー名が含まれない。
- `cached_tokens` が存在しないレスポンスでも落ちない。

## 採用済み判断

| ID | 判断 | 理由 |
|---|---|---|
| D-001 | 新規圧縮器ではなく既存Headroomにprofile追加 | 影響範囲が小さく、既存CLIとproxyを活かせる |
| D-002 | 初期はlossless-first | Codex誤読リスクを下げる |
| D-003 | safe-codexはcache-first | Prompt Cachingを壊さないため |
| D-004 | 本文ログを禁止 | API key、個人情報、職場情報の残存リスクを下げる |
| D-005 | `AGENTS.md` 自動書き換えを抑制 | Codex運用ルールの正本を壊さないため |

## 未決定事項

| ID | 未決定事項 | 判断タイミング |
|---|---|---|
| U-001 | `safe-codex` profileの実装場所 | Phase 1 |
| U-002 | `--profile` optionの追加位置 | Phase 1 |
| U-003 | `prompt_cache_key` 注入箇所 | Phase 3 |
| U-004 | `cached_tokens` 集計箇所 | Phase 3 |
| U-005 | `learn --apply` 抑制の実装方法 | Phase 5 |

## 設計更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | 初期設計方針を作成 |
