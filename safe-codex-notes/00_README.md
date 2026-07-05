# headroom-safe-codex 情報源 README

このフォルダは、Headroom改変版 `headroom-safe-codex` を新規プロジェクトとして進めるための情報源です。  
目的は、新規チャット・別セッション・将来の再開時に、背景・設計判断・進捗を短時間で復元できる状態にすることです。

## ファイル構成

```text
headroom-safe-codex-best-source/
├─ 00_README.md
├─ 01_PROJECT_CONTEXT.md
├─ 02_DESIGN_DECISIONS.md
└─ 03_ROADMAP_PROGRESS.md
```

| ファイル | 役割 | 更新タイミング |
|---|---|---|
| `00_README.md` | この情報源の使い方、更新ルール、禁止事項 | 運用ルールを変えたとき |
| `01_PROJECT_CONTEXT.md` | プロジェクト背景、目的、環境、基準commit、Phase 0の実績 | 環境・前提・upstream基準が変わったとき |
| `02_DESIGN_DECISIONS.md` | safe-codexの設計方針、禁止事項、Prompt Caching方針、テスト方針 | 設計判断を変更したとき |
| `03_ROADMAP_PROGRESS.md` | Phase別進捗、完了条件、残課題、リスク | 各Phase完了時・計画変更時 |

## 読む順番

作業再開時は、以下の順で読む。

1. `03_ROADMAP_PROGRESS.md`
2. `01_PROJECT_CONTEXT.md`
3. `02_DESIGN_DECISIONS.md`

理由:

- 最初に現在のPhaseと次アクションを確認する。
- 次に現在の環境・base tag / commitを確認する。
- 最後に設計上の制約と禁止事項を確認する。

## 更新ルール

### Phaseが進んだとき

必ず `03_ROADMAP_PROGRESS.md` を更新する。

更新対象:

- Phase一覧
- 現在のPhase
- 達成度
- 実施内容
- 成果物
- 残課題
- リスク管理表
- 更新履歴

### 環境・基準commitが変わったとき

`01_PROJECT_CONTEXT.md` を更新する。

更新対象:

- local path
- branch
- base tag
- base commit
- Python / Rust / maturin / Codex CLI
- Phase 0相当の環境情報

### 設計判断が変わったとき

`02_DESIGN_DECISIONS.md` を更新する。

更新対象:

- safe-codex既定値
- 禁止または明示許可が必要な機能
- Prompt Caching方針
- ログ方針
- test方針
- 採用 / 不採用判断

## 書いてはいけない情報

この情報源には以下を書かない。

```text
OpenAI API key
Anthropic API key
GitHub token
GitHub PAT
Authorization header
個人情報
職場情報
医療情報
患者情報
実データのログ全文
request / response本文
公開したくないローカル絶対パス
```

## Git運用メモ

`safe-codex-base` は基準保存用ブランチとして扱う。  
実作業はPhaseごとに作業ブランチを切る。

例:

```powershell
cd C:\dev\headroom-safe-codex
git checkout safe-codex-base
git checkout -b safe-codex/phase1-design
```

原則:

- `safe-codex-base` では直接作業しない。
- Phase単位で作業ブランチを作る。
- commit前に `git status` とテスト結果を確認する。
- pushは必要になった時点で判断する。

## 作業再開時の最小確認

```powershell
cd C:\dev\headroom-safe-codex
git status --short --branch
git log -1 --oneline
python --version
```

期待されるPython:

```text
Python 3.12.13
```

仮想環境が有効でない場合:

```powershell
.venv\Scripts\Activate.ps1
python --version
```

## 現在の状態

| 項目 | 内容 |
|---|---|
| 現在の完了Phase | Phase 0 |
| 次のPhase | Phase 1: safe-codex詳細設計 |
| local path | `C:\dev\headroom-safe-codex` |
| upstream | `https://github.com/headroomlabs-ai/headroom.git` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| base branch | `safe-codex-base` |

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

