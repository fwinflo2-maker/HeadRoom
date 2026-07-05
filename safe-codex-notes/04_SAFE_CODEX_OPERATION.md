# safe-codex 運用手順

## 目的

この文書は、`headroom-safe-codex` をWindowsローカルで安全に使うための最小運用手順をまとめる。

対象:

- 導入手順
- 安全設定の理由
- 危険オプション
- Windows検証手順
- 切り戻し方法

前提:

- 個人ローカル運用を対象にする。
- 外部公開プロキシ化は対象外。
- 医療情報、職場情報、患者情報、実データのログ本文は扱わない。
- API key、token、Authorization headerを本文・ログ・ドキュメントに残さない。

## 1. 導入前の確認

PowerShell 5.1で確認する。

~~~powershell
cd C:\dev\headroom-safe-codex
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .venv\Scripts\Activate.ps1
}

git status --short --branch
git log -1 --oneline
python --version
headroom --help
~~~

期待する状態:

~~~text
branch: safe-codex/phase2-safe-profile
Python: 3.12.13
worktree: ソース差分なし
~~~

`phase*_investigation/` は一時調査フォルダのため、原則commitしない。

## 2. safe-codex proxyの起動

通常の起動例:

~~~powershell
cd C:\dev\headroom-safe-codex
$env:PYTHONUTF8 = "1"

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .venv\Scripts\Activate.ps1
}

headroom proxy --profile safe-codex --host 127.0.0.1 --port 8787
~~~

注意:

- `--host 127.0.0.1` を使う。
- `--host 0.0.0.0` は使わない。
- `--log-messages` は使わない。
- `--codex-wire-debug` は使わない。
- `--no-open` は実CLIに存在しないため使わない。

## 3. Codexをsafe-codex経由で起動する

通常の起動例:

~~~powershell
cd C:\dev\headroom-safe-codex
$env:PYTHONUTF8 = "1"

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .venv\Scripts\Activate.ps1
}

headroom wrap codex --safe --prompt-cache-key auto --prompt-cache-retention in_memory
~~~

目的:

- `--safe` でsafe-codex profileを使う。
- `--prompt-cache-key auto` でローカル絶対パスやユーザー名を送らないopaque keyを使う。
- `--prompt-cache-retention in_memory` で長期保持を避ける。

## 4. 安全設定の理由

| 設定 | 理由 |
|---|---|
| `HEADROOM_MODE=cache` | Prompt Cachingを壊しにくくする |
| `HEADROOM_LOSSLESS=1` | Codexの誤読リスクを下げる |
| `HEADROOM_DISABLE_KOMPRESS=1` | 圧縮による意味変化を避ける |
| `HEADROOM_LOG_MESSAGES=0` | request / response本文を残さない |
| `HEADROOM_HOST=127.0.0.1` | proxyを外部公開しない |
| `prompt_cache_key=auto` | 絶対パス、ユーザー名、tokenをcache keyへ入れない |
| `prompt_cache_retention=in_memory` | 長期保持による情報残存リスクを下げる |
| `headroom learn --apply` 明示許可制 | `AGENTS.md` / `instructions.md` の無確認書き換えを防ぐ |

## 5. 危険オプション

safe-codex運用では以下を使わない。

| 対象 | 扱い | 理由 |
|---|---|---|
| `--host 0.0.0.0` | 禁止 | ローカルproxyを外部公開するリスク |
| 非loopback host | 禁止 | LANや外部から到達されるリスク |
| `HEADROOM_HOST=0.0.0.0` | 禁止 | 環境変数経由の外部公開リスク |
| `--log-messages` | 禁止 | request / response本文が残る |
| `HEADROOM_LOG_MESSAGES=1` | 禁止 | 本文ログが有効化される |
| `--codex-wire-debug` | 禁止 | Codex通信内容が保存される |
| `--codex-wire-debug-dir` | 禁止 | Codex通信内容の保存先を作る |
| `HEADROOM_CODEX_WIRE_DEBUG=1` | 禁止 | 環境変数経由でwire debugが有効化される |
| `wrap codex --safe --memory` | 禁止 | 永続memory/context file書き込みリスク |
| `headroom learn --apply` with safe-codex | 原則禁止 | `AGENTS.md` / `instructions.md` の無確認書き換えリスク |
| `--prompt-cache-retention 24h` | 慎重扱い | 情報が長く保持される可能性がある |
| `--openai-api-url` | 検証用途中心 | 信頼できないendpointにAPI keyや内容を送るリスク |

`headroom learn --apply --allow-context-write` は、意図してcontext/memory fileを書き換える場合のみ使う。

## 6. Windows検証手順

### 6.1 CLI help確認

実装後やPhase再開時は、先に実CLI helpを確認する。

~~~powershell
headroom proxy --help
headroom wrap codex --help
headroom learn --help
~~~

確認する主なoption:

~~~text
headroom proxy --profile safe-codex
headroom proxy --prompt-cache-key
headroom proxy --prompt-cache-retention
headroom proxy --openai-api-url
headroom proxy --host
headroom proxy --log-messages
headroom proxy --codex-wire-debug
headroom wrap codex --safe
headroom wrap codex --prompt-cache-key
headroom wrap codex --prompt-cache-retention
headroom learn --allow-context-write
~~~

### 6.2 focused test

safe-codex関連の最小確認:

~~~powershell
python -m pytest tests/test_safe_codex_profile.py tests/test_cli_safe_codex.py tests/test_cli_learn.py
python -m pytest tests/test_backends/test_litellm_cache_stats.py tests/test_proxy/test_openai_backend_path.py
~~~

関連確認:

~~~powershell
python -m pytest tests/test_cli_proxy_env.py tests/test_proxy_loopback_gating.py tests/test_provider_codex_runtime.py
~~~

### 6.3 lint / format / diff check

~~~powershell
ruff check headroom tests
ruff format --check headroom tests
git diff --check
git status --short --branch
~~~

注意:

- `ruff check .` は `phase*_investigation/` の一時scriptを拾うため避ける。
- Windowsで多数ファイルをruffへ個別展開すると引数長制限に当たるため、`ruff check headroom tests` を優先する。
- `git diff --check` はCRLF warningを出すことがあるため、whitespace errorとwarningを分けて判断する。

### 6.4 OpenAI backend path検証

fake OpenAI endpointを使う場合のみ、`--openai-api-url` を指定する。

~~~powershell
headroom proxy `
    --profile safe-codex `
    --backend openai `
    --openai-api-url http://127.0.0.1:<fake-openai-port>/v1 `
    --prompt-cache-key auto `
    --prompt-cache-retention in_memory `
    --host 127.0.0.1 `
    --port 8787
~~~

確認すること:

- fake OpenAIへroutingされる。
- `prompt_cache_key` が上流request bodyに入る。
- `prompt_cache_retention` が上流request bodyに入る。
- `cached_tokens` が取得できる場合にstatsへ反映される。
- stdout / stderr / health / stats にprompt本文、Authorization、API keyが出ない。
- 通常profileの挙動を壊していない。

## 7. 切り戻し方法

### 7.1 実行中proxyを止める

proxyを起動しているPowerShellで `Ctrl+C` を押す。

### 7.2 一時環境変数を消す

~~~powershell
Remove-Item Env:\HEADROOM_PROFILE -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_MODE -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_LOSSLESS -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_DISABLE_KOMPRESS -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_LOG_MESSAGES -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_HOST -ErrorAction SilentlyContinue
Remove-Item Env:\HEADROOM_CODEX_WIRE_DEBUG -ErrorAction SilentlyContinue
Remove-Item Env:\OPENAI_BASE_URL -ErrorAction SilentlyContinue
~~~

### 7.3 作業差分だけ戻す

未commit差分を戻す前に必ず確認する。

~~~powershell
git status --short --branch
git diff --stat
~~~

safe-codex docsだけ戻す例:

~~~powershell
git restore -- safe-codex-notes/00_README.md
Remove-Item safe-codex-notes\04_SAFE_CODEX_OPERATION.md -ErrorAction SilentlyContinue
~~~

### 7.4 基準branchへ戻る

~~~powershell
git switch safe-codex-base
git status --short --branch
~~~

### 7.5 commit済み変更を戻す場合

`git reset --hard` は破壊的操作のため、実行前に対象commitと影響範囲を確認する。

確認:

~~~powershell
git log --oneline -5
git status --short --branch
~~~

原則として、いきなり `git reset --hard` しない。

## 8. 再開時の最小確認

~~~powershell
cd C:\dev\headroom-safe-codex
$env:PYTHONUTF8 = "1"

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . .venv\Scripts\Activate.ps1
}

git status --short --branch
git log -1 --oneline
python --version
headroom proxy --help
headroom wrap codex --help
~~~

確認後、`safe-codex-notes/03_ROADMAP_PROGRESS.md`、`safe-codex-notes/01_PROJECT_CONTEXT.md`、`safe-codex-notes/02_DESIGN_DECISIONS.md`、この文書の順で読む。