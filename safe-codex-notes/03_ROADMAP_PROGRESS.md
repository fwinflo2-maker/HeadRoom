# ROADMAP_PROGRESS

## 運用方針

このファイルは、プロジェクトの達成度に応じて更新する。  
各Phase完了時に、状態・達成度・実施内容・未完了事項を更新する。

## 現在の状態

| 項目 | 内容 |
|---|---|
| 現在の完了Phase | Phase 0 |
| 現在の作業前状態 | Phase 1未開始 |
| 次のPhase | Phase 1: safe-codex詳細設計 |
| base branch | `safe-codex-base` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |

## Phase一覧

| Phase | 状態 | 達成度 | 目的 |
|---:|---|---:|---|
| 0 | 完了 | 100% | upstream固定・作業環境確認 |
| 1 | 未開始 | 0% | `safe-codex`設計・変更対象確定 |
| 2 | 未開始 | 0% | `safe-codex` profileとCLI追加 |
| 3 | 未開始 | 0% | Prompt Caching対応 |
| 4 | 未開始 | 0% | ログ安全化 |
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

状態: 未開始  
達成度: 0%

### 目的

実装前に既存構成を確認し、`safe-codex` profileを最小変更で追加する設計を確定する。

### 確認対象

```text
headroom/cli/proxy.py
headroom/cli/wrap.py
headroom/providers/codex/runtime.py
tests/
pyproject.toml
```

### 完了条件

- 変更ファイル候補が決まっている。
- 新規ファイル候補が決まっている。
- safe-codexの既定値が決まっている。
- 禁止する危険オプションが決まっている。
- テスト項目が決まっている。
- 通常profileの既存挙動を壊さない設計になっている。

### 成果物予定

- Phase 2に渡せる実装対象リスト
- 追加ファイル案
- 変更ファイル案
- テスト方針
- 未決定事項の整理

## Phase 2: safe-codex最小実装

状態: 未開始  
達成度: 0%

### 目的

`headroom proxy --profile safe-codex` と `headroom wrap codex --safe` を最小実装する。

### 実装対象

- safe-codex profile追加
- `--profile safe-codex`
- `wrap codex --safe`
- loopback制限
- 危険ログオプション拒否
- 最小テスト追加

### 完了条件

- `--profile safe-codex` が認識される。
- `wrap codex --safe` が認識される。
- `OPENAI_BASE_URL` は既存通り設定される。
- safeではhost `127.0.0.1` 以外を拒否する。
- safeでは `--log-messages` / `--codex-wire-debug` を拒否する。
- 既存の通常profileを壊していない。

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

- `--log-messages` 禁止
- `--codex-wire-debug` 禁止
- redact関数追加
- 数値メトリクス中心のログ設計

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
| R-001 | 圧縮でCodexが誤読する | 高 | 初期はlossless-first / Kompress無効 | 未確認 |
| R-002 | request/response本文がログに残る | 高 | `--log-messages` 禁止 | 未実装 |
| R-003 | Codex wire debugに機密情報が残る | 高 | safe profileで禁止 | 未実装 |
| R-004 | `AGENTS.md` が自動書き換えされる | 中 | `learn --apply` 明示許可制 | 未実装 |
| R-005 | Prompt Cachingが壊れる | 中 | `cache-first` / stable prefix | 未実装 |
| R-006 | prompt_cache_keyに生パスが入る | 中 | hash化 | 未実装 |
| R-007 | Windowsで起動しない | 高 | Phase 6で手動検証 | 未確認 |
| R-008 | 通常Headroom挙動を壊す | 高 | safe明示時のみ新挙動 | 未確認 |

## 更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | Phase 0完了内容を反映 |
