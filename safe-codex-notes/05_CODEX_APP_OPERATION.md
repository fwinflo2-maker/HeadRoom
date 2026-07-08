# Codex Desktop Actions 運用手順

## 目的

この文書は、Codex Desktop Actions から `safe-codex` proxy を起動・確認・停止しやすくするための手順をまとめる。

対象:

- proxy起動script
- proxy状態確認script
- proxy停止と一時環境変数削除script
- Codex Desktop Actions へ登録するコマンド例

対象外:

- `~/.codex/config.toml` の変更
- `~/.codex/.env` の変更
- API key の保存
- Codex Skill 化
- GitHub push

## 前提

- Windows / PowerShell 5.1 を前提にする。
- repo root は `C:\dev\headroom-safe-codex` を想定する。
- `.venv\Scripts\Activate.ps1` があればscript側で有効化する。
- `$env:PYTHONUTF8 = "1"` をscript側で設定する。
- proxyは `127.0.0.1` のみにbindする。
- `prompt_cache_retention` は通常 `in_memory` を使う。

## 作成したscript

| script | 目的 |
|---|---|
| `scripts/start-safe-codex-proxy.ps1` | `safe-codex` proxyを `127.0.0.1:8787` で起動する |
| `scripts/check-safe-codex-status.ps1` | port 8787 のlisten状態とprocessを確認する |
| `scripts/stop-safe-codex-env.ps1` | safe-codex proxy processを安全側で停止し、一時環境変数を削除する |

## Codex Desktop Actions 登録例

### Start safe-codex proxy

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dev\headroom-safe-codex\scripts\start-safe-codex-proxy.ps1
~~~

### Check safe-codex proxy

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dev\headroom-safe-codex\scripts\check-safe-codex-status.ps1
~~~

### Stop safe-codex proxy

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\dev\headroom-safe-codex\scripts\stop-safe-codex-env.ps1
~~~

## 起動scriptの実行内容

`start-safe-codex-proxy.ps1` は以下を行う。

1. repo rootへ移動する。
2. `$env:PYTHONUTF8 = "1"` を設定する。
3. `.venv\Scripts\Activate.ps1` があれば有効化する。
4. 危険なwire debug保存先環境変数を削除する。
5. port 8787 が既にlisten中なら重複起動しない。
6. 以下の安全側コマンドでproxyを起動する。

~~~powershell
headroom proxy `
    --profile safe-codex `
    --host 127.0.0.1 `
    --port 8787 `
    --prompt-cache-key auto `
    --prompt-cache-retention in_memory
~~~

## 禁止事項

Codex Desktop Actions へ以下を登録しない。

| 禁止対象 | 理由 |
|---|---|
| `--host 0.0.0.0` | proxyを外部公開するリスクがある |
| `--log-messages` | request / response本文が残る |
| `--codex-wire-debug` | Codex通信内容が保存される |
| `--codex-wire-debug-dir` | Codex通信内容の保存先を作る |
| API keyやtokenを含むコマンド | Actions設定や履歴に秘密情報が残る |

## 状態確認

通常確認:

~~~powershell
scripts\check-safe-codex-status.ps1
~~~

想定される正常出力:

~~~text
safe-codex proxy status: listening on loopback.
~~~

停止中の場合、exit code は `1` になる。

非loopback addressでlistenしている場合、exit code は `2` になる。
この場合は安全でない可能性があるため、processと起動引数を確認する。

## 停止

通常停止:

~~~powershell
scripts\stop-safe-codex-env.ps1
~~~

このscriptは、port 8787 をlistenしているprocessのcommand lineが `headroom proxy` かつ `safe-codex` に見える場合のみ停止する。
一致しないprocessは停止しない。

環境変数だけ削除したい場合:

~~~powershell
scripts\stop-safe-codex-env.ps1 -SkipProcessStop
~~~

## 運用手順

1. Codex Desktop Actions で `Start safe-codex proxy` を実行する。
2. 別ActionまたはPowerShellで `Check safe-codex proxy` を実行する。
3. Codex側の接続確認を行う。
4. 作業終了時に `Stop safe-codex proxy` を実行する。

## 注意

- このPhaseでは `~/.codex/config.toml` は変更しない。
- このPhaseでは `~/.codex/.env` は変更しない。
- API key、token、Authorization header、prompt本文、response本文をこの文書やログへ残さない。
- 実行中proxyを止める最も確実な方法は、起動したPowerShellで `Ctrl+C` を押すこと。