# PROJECT_CONTEXT

## プロジェクト名

`headroom-safe-codex`

## 目的

`headroomlabs-ai/headroom` をベースに、個人のCodex運用向けに安全・軽量・cache-firstな改変版を作成する。

## 主な目的

- Codex利用時の入力トークン消費を抑える。
- OpenAI Prompt Cachingを壊さない。
- ログ・APIキー・個人情報・職場情報の漏洩リスクを下げる。
- `AGENTS.md` / `instructions.md` の自動書き換えを抑制する。
- Windowsローカル環境で安定して使える形にする。
- 最大限Codexを使わず、ChatGPTで設計・差分作成・レビュー・テスト方針作成を行う。

## 非目標

初期段階では以下を行わない。

- 圧縮アルゴリズム自体の全面改修
- 新しいLLMプロキシ基盤の作成
- チーム向けSaaS化
- 外部公開プロキシ化
- 医療情報・職場情報を含む実データでの検証
- `headroom learn --apply` の自動常用
- `pip install headroom-ai[all]` 前提の重い構成

## upstream / local

| 項目 | 内容 |
|---|---|
| upstream | `https://github.com/headroomlabs-ai/headroom.git` |
| local path | `C:\dev\headroom-safe-codex` |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| base branch | `safe-codex-base` |
| Phase 0状態 | 完了 |

## 現在の環境

| 項目 | 状態 |
|---|---|
| OS | Windows 11 |
| PowerShell | 5.1 |
| Git | OK |
| Python | 3.12.13 / `.venv` |
| uv | OK |
| Rust / cargo | OK |
| rustup | OK |
| maturin | OK |
| Codex CLI | OK |
| Visual Studio Build Tools | OK |
| MSVC `cl.exe` | OK via `vcvars64.bat` |
| `link.exe` | OK via `vcvars64.bat` |
| Execution Policy | `CurrentUser RemoteSigned` に変更済み |

## Phase 0実施済み内容

### 環境確認済み

- Git
- Python 3.12
- uv
- Rust / cargo
- rustup
- maturin
- Codex CLI
- Visual Studio Build Tools
- MSVC compiler / linker

### Python

| 項目 | 内容 |
|---|---|
| 使用バージョン | Python 3.12.13 |
| 導入方法 | `uv` |
| 備考 | このプロジェクトではPython 3.12を使用する |

### Rust / cargo

| 項目 | 内容 |
|---|---|
| rustc | `1.96.1` |
| cargo | `1.96.1` |
| rustup | `1.29.0` |

### Visual Studio Build Tools

| 項目 | 内容 |
|---|---|
| Build Tools path | `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools` |
| MSVC tools | 確認済み |
| `cl.exe` | 確認済み |
| `link.exe` | 確認済み |
| compiler version | `19.44.35228 for x64` |

通常PowerShellでは `cl` / `link` が見えないが、`vcvars64.bat` 経由で確認済みのため問題なし。

必要時:

```powershell
cmd /k "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
```

### maturin

| 項目 | 内容 |
|---|---|
| maturin | `1.14.1` |

### Headroom clone / base固定

| 項目 | 内容 |
|---|---|
| repository clone | 完了 |
| base tag | `v0.30.0` |
| base commit | `728b3308` |
| base branch | `safe-codex-base` |
| current Python | `3.12.13` |

## Headroom現状メモ

### upstreamの重要ポイント

- `headroomlabs-ai/headroom` はAIエージェントが読むtool outputs、logs、RAG chunks、files、conversation historyなどをLLM送信前に圧縮する。
- `wrap codex` に対応している。
- `headroom proxy` によるOpenAI互換プロキシとして利用できる。
- `--mode cache`、`--lossless`、`--disable-kompress` など、今回の目的に近い既存オプションがある。

### 既存懸念

- バージョン情報やドキュメントの整合性確認が必要。
- 圧縮によるCodex誤読リスクがある。
- `--log-messages` やwire debugで機密情報が残る可能性がある。
- `headroom learn` はCodexの場合 `AGENTS.md` / `instructions.md` に書き込む設計のため、safe profileでは抑制が必要。
- Prompt Cachingと圧縮は衝突する場合があるため、cache-first設計が必要。

## 更新履歴

| 日付 | Phase | 更新内容 |
|---|---:|---|
| 2026-07-05 | 0 | 初期環境確認、base tag固定、Python 3.12仮想環境作成まで完了 |

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


<!-- PHASE6-OPERATION-RULES-START -->
## Phase 6運用ルール追記

- PowerShell 5.1では bash here-doc 形式の `python - <<'PY'` を使わない。
- Pythonを使う場合は、一時 `.py` ファイルをASCIIで生成して実行する。
- 日本語Markdown更新はPowerShell側で行い、`.NET WriteAllText(..., UTF8Encoding(false))` を使う。
- Markdown本文にバッククォートを含む場合は、PowerShellの double-quoted here-string を使わない。
- 正本Markdown更新時は相対パスではなく `Resolve-Path` 済みの絶対パスを `.NET File API` へ渡す。
- CLI optionは実行前に `headroom proxy --help` / `headroom wrap ... --help` と照合する。
- `git diff --check` はCRLF warningを出すことがあるため、実エラーとwarningを分けて判断する。
- Windowsで多数ファイルをruffへ個別展開すると引数長制限に当たるため、`ruff check headroom tests` を優先する。
<!-- PHASE6-OPERATION-RULES-END -->
