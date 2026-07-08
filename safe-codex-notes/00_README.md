# headroom-safe-codex 情報源 README

このフォルダは、Headroom改変版 `headroom-safe-codex` を新規プロジェクトとして進めるための情報源です。  
目的は、新規チャット・別セッション・将来の再開時に、背景・設計判断・進捗を短時間で復元できる状態にすることです。

## ファイル構成

```text
headroom-safe-codex-best-source/
├─ 00_README.md
├─ 01_PROJECT_CONTEXT.md
├─ 02_DESIGN_DECISIONS.md
├─ 03_ROADMAP_PROGRESS.md
├─ 04_SAFE_CODEX_OPERATION.md
└─ 05_CODEX_APP_OPERATION.md
```

| ファイル | 役割 | 更新タイミング |
|---|---|---|
| `00_README.md` | この情報源の使い方、更新ルール、禁止事項 | 運用ルールを変えたとき |
| `01_PROJECT_CONTEXT.md` | プロジェクト背景、目的、環境、基準commit、Phase 0の実績 | 環境・前提・upstream基準が変わったとき |
| `02_DESIGN_DECISIONS.md` | safe-codexの設計方針、禁止事項、Prompt Caching方針、テスト方針 | 設計判断を変更したとき |
| `03_ROADMAP_PROGRESS.md` | Phase別進捗、完了条件、残課題、リスク | 各Phase完了時・計画変更時 |
| `04_SAFE_CODEX_OPERATION.md` | safe-codexの導入、危険オプション、Windows検証、切り戻し手順 | 運用手順を変えたとき |
| `05_CODEX_APP_OPERATION.md` | Codex Desktop Actions からの起動・確認・停止手順 | Codexアプリ側の運用手順を変えたとき |

## 読む順番

作業再開時は、以下の順で読む。

1. `03_ROADMAP_PROGRESS.md`
2. `01_PROJECT_CONTEXT.md`
3. `02_DESIGN_DECISIONS.md`
4. `04_SAFE_CODEX_OPERATION.md`
5. `05_CODEX_APP_OPERATION.md`

理由:

- 最初に現在のPhaseと次アクションを確認する。
- 次に現在の環境・base tag / commitを確認する。
- 最後に設計上の制約、禁止事項、実運用手順を確認する。

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

## 正本Markdownの保管場所と更新ルール

`C:\dev\headroom-safe-codex\safe-codex-notes` を、このプロジェクトの6つの正本Markdownの保管場所とする。

対象ファイル:

- `00_README.md`
- `01_PROJECT_CONTEXT.md`
- `02_DESIGN_DECISIONS.md`
- `03_ROADMAP_PROGRESS.md`
- `04_SAFE_CODEX_OPERATION.md`
- `05_CODEX_APP_OPERATION.md`

Phase完了ごとに行うこと:

1. `safe-codex-notes/` 配下の6ファイルを確認する。
2. Phase状態、latest Phase commit、実施内容、確認結果、残課題、失敗・改善策を必要最小限で反映する。
3. 恒常的な運用変更は `00_README.md` に反映する。
4. 設計判断へ影響する内容は `02_DESIGN_DECISIONS.md` に反映する。
5. 実運用手順の変更は `04_SAFE_CODEX_OPERATION.md` または `05_CODEX_APP_OPERATION.md` に反映する。
6. `03_ROADMAP_PROGRESS.md` はPhase完了時に必ず更新する。
7. `phase*_investigation/` は調査用一時ファイルとして扱い、commitしない。
8. ChatGPTプロジェクト等へ情報源として渡す場合は、更新後の `safe-codex-notes/` 内6ファイルを使う。

注意:

- API key、token、Authorization header、prompt本文、response本文は記録しない。
- 長いログ全文は正本Markdownに貼らず、必要な事実だけを記録する。
- Phase完了後の情報源更新は、実装commitまたはdocs commit後に行う。

### commit記録ルール

正本Markdown内では、docs commit自身のhashを `latest commit` として無理に記録しない。

理由:

- commit hashはファイル内容から決まるため、docs内にそのcommit自身のhashを完全に書くことは安定しない。
- 最新HEADは `git log -1 --oneline` で確認する。

記録方針:

- `03_ROADMAP_PROGRESS.md` には `latest Phase work commit` や `latest source Markdown refresh commit` のように、意味を明確にしたcommitを記録する。
- Phase完了後は `git log -1 --oneline` を確認し、必要なら次回docs更新時に記録する。
- 古い `latest commit` 表記が残っていないか、Phase完了時に確認する。

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
| 現在の完了Phase | Phase 9-B |
| 次のPhase | Phase 9-C: Codex Skill化 |
| local path | `C:\dev\headroom-safe-codex` |
| upstream | `https://github.com/headroomlabs-ai/headroom.git` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| base branch | `safe-codex-base` |

## Phase 4以降の標準作業フロー

Phase 4以降は、Phase 3 / Phase 3-Bで安定した以下の流れを標準とする。

```text
1. 新規Phaseは新規チャットで開始
2. 6つの正本Markdownを参照
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


## Phase完了時の失敗・改善反映ルール

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

## 長い出力のファイル化ルール

調査コマンド、grep、diff、ログ抽出、コード断片抽出など、結果出力が長くなる可能性がある作業では、原則として画面に直接全文を出さない。

運用:

- `phase*_investigation/` 配下に結果ファイルを作成する。
- 画面には作成ファイルパス、件数、短い要約のみ表示する。
- 必要に応じて、そのファイルをChatGPTへアップロードする。
- `Get-Content` で全文表示する指示は原則避ける。
- 貼り付けが必要な場合も、必要範囲だけを抽出する。
- commit対象には `phase*_investigation/` を含めない。

<!-- PHASE6-WINDOWS-NOTE-START -->
## Phase 6 Windows検証メモ

Windowsローカル検証により、`safe-codex` profile の OpenAI backend path で以下を確認済み。

- loopback proxy起動
- fake OpenAIへのrouting
- `--openai-api-url` の実送信反映
- `prompt_cache_key` / `prompt_cache_retention` の上流送信
- `cached_tokens` の取り込み
- prompt本文 / API key / Authorization のログ非露出
- 通常profile互換性
<!-- PHASE6-WINDOWS-NOTE-END -->
