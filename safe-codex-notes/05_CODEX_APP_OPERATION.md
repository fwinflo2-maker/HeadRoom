# Codex Desktop Actions 運用手順

## 目的

この文書は、Codex Desktop Actions から `safe-codex` proxy を起動・確認・停止しやすくするための手順をまとめる。

対象:

- proxy起動script
- proxy状態確認script
- proxy停止と一時環境変数削除script
- Codex Desktop Actions へ登録するコマンド例
- Codex custom model provider 設定確認

対象外:

- `~/.codex/config.toml` へのAPI key保存や秘密情報の追記
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

## Codex custom model provider 設定

Phase 9-Bでは、既存の `[model_providers.headroom]` を使い、user-level `~/.codex/config.toml` に top-level provider selection を追加した。

設定方針:

- `model_provider = "headroom"` を使う。
- 既存 `[model_providers.headroom]` を使う。
- `base_url` は `http://127.0.0.1:8787/v1` とする。
- `~/.codex/.env` は変更しない。
- API key、token、Authorization headerを `config.toml` やdocsへ書かない。
- 新規 `[model_providers.safe_codex]` は追加しない。既存providerの重複を避けるため。

検証結果:

- `config.toml` に `model_provider = "headroom"` が存在することを確認した。
- `[model_providers.headroom]` と対象 `base_url` が存在することを確認した。
- `0.0.0.0`、log / wire debug系、token風文字列がないことを確認した。
- `safe-codex` proxy は `127.0.0.1:8787` でlistenし、`/health` が `200` を返すことを確認した。
- `codex exec` は `provider: headroom` を認識し、usage limit解除後にcompletion成功も確認した。
- completion成功はusage limit解除後に確認済み。
- prompt本文を含む可能性がある一時stderr artifactは削除した。

注意:

- `codex exec` のstdout / stderrをそのままファイル保存すると、prompt本文が残る場合がある。
- 接続確認では、prompt本文やresponse本文を保存しない。
- 失敗時も、error全文ではなく、exit code、provider名、usage limit有無などの事実だけを抽出して記録する。`r`n- completion確認時も、prompt本文・response本文・stderr本文は保存しない。

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

- Phase 9-Bでは `~/.codex/config.toml` に provider 選択のみ追加した。API keyやtokenは書かない。
- このPhaseでは `~/.codex/.env` は変更しない。
- API key、token、Authorization header、prompt本文、response本文をこの文書やログへ残さない。
- 実行中proxyを止める最も確実な方法は、起動したPowerShellで `Ctrl+C` を押すこと。

## Codex Skill

Phase 9-Cで、成功した運用手順を次のSkillに集約する。

- skills/safe-codex-codex-app/SKILL.md

このSkillは、Codex Desktop Actionsからのproxy起動・確認・停止、headroom custom provider確認、completion検証時の保存禁止事項を扱う。

Skillに含める内容は運用手順と安全制約のみとし、API key、token、Authorization header、prompt本文、response本文、stderr本文は含めない。
