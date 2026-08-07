<div align="center"><pre>
  ██╗  ██╗███████╗ █████╗ ██████╗ ██████╗  ██████╗  ██████╗ ███╗   ███╗
  ██║  ██║██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██╔═══██╗████╗ ████║
  ███████║█████╗  ███████║██║  ██║██████╔╝██║   ██║██║   ██║██╔████╔██║
  ██╔══██║██╔══╝  ██╔══██║██║  ██║██╔══██╗██║   ██║██║   ██║██║╚██╔╝██║
  ██║  ██║███████╗██║  ██║██████╔╝██║  ██║╚██████╔╝╚██████╔╝██║ ╚═╝ ██║
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝
              AI agent 的上下文壓縮層
</pre></div>

<p align="center"><strong>減少 60–95% tokens(JSON 資料)、減少 15-20% tokens(coding agent)· library · proxy · MCP · 內容感知壓縮器 · local-first · 可逆</strong></p>

<p align="center"><sub><a href="README.md">English</a></sub></p>

<p align="center">
  <a href="https://github.com/chopratejas/headroom/actions/workflows/ci.yml"><img src="https://github.com/chopratejas/headroom/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://app.codecov.io/gh/chopratejas/headroom"><img src="https://codecov.io/gh/chopratejas/headroom/graph/badge.svg" alt="codecov"></a>
  <a href="https://pypi.org/project/headroom-ai/"><img src="https://img.shields.io/pypi/v/headroom-ai.svg" alt="PyPI"></a>
  <a href="https://www.npmjs.com/package/headroom-ai"><img src="https://img.shields.io/npm/v/headroom-ai.svg" alt="npm"></a>
  <a href="https://huggingface.co/chopratejas/kompress-v2-base"><img src="https://img.shields.io/badge/model-Kompress--v2--base-yellow.svg" alt="Model: Kompress-v2-base"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0"></a>
  <a href="https://headroom-docs.vercel.app/docs"><img src="https://img.shields.io/badge/docs-online-blue.svg" alt="Docs"></a>
</p>

<!-- mcp-name: io.github.headroomlabs-ai/headroom -->

<p align="center">
  <a href="https://headroom-docs.vercel.app/docs">文件</a> ·
  <a href="#get-started60-秒">安裝</a> ·
  <a href="#proof成效證明">成效證明</a> ·
  <a href="#agent-相容性矩陣">Agents</a> ·
  <a href="https://discord.gg/yRmaUNpsPJ">Discord</a> ·
  <a href="llms.txt">llms.txt</a>
</p>

<p align="center"><sub>
  <b>AI agents / LLMs:</b> 請閱讀 <a href="llms.txt"><code>/llms.txt</code></a>,或抓取 <a href="https://headroom-docs.vercel.app/llms.txt">線上索引</a> / <a href="https://headroom-docs.vercel.app/llms-full.txt">完整文件內容</a>。
</sub></p>

---
<p align="center"><a href="https://trendshift.io/repositories/20881" target="_blank"><img src="https://trendshift.io/api/badge/repositories/20881" alt="chopratejas%2Fheadroom | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a></p>

Headroom 會在你的 AI agent 讀取的所有內容送進 LLM 之前先壓縮它——工具輸出、日誌、RAG 片段、檔案、對話歷史。答案不變,tokens 大幅減少。

<p align="center">
  <img src="HeadroomDemo-Fast.gif" alt="Headroom in action" width="820">
  <br/><sub>實際案例:10,144 → 1,260 tokens——同樣找到 FATAL 錯誤。</sub>
</p>

## 它做什麼

- **Library** — Python 或 TypeScript 中的 `compress(messages)`,可內嵌於任何應用程式
- **Proxy** — `headroom proxy --port 8787`,零程式碼修改,支援任何語言
- **Agent wrap** — 一行指令 `headroom wrap claude|codex|grok|copilot|cursor|aider|opencode|cline|continue|goose|openhands|openclaw|vibe|omp|zcode`;用 `headroom unwrap <tool>` 復原
- **MCP server** — 提供 `headroom_compress`、`headroom_retrieve`、`headroom_stats` 給任何 MCP client
- **跨 agent 記憶** — Claude、Codex、Gemini、Grok 共用的儲存體,自動去重
- **`headroom learn`** — 挖掘失敗的 session,將修正寫入 `CLAUDE.local.md`(預設,已加入 gitignore)或 `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `GROK.md`
- **輸出 token 減少** — 減少模型「寫回」的內容(不只是你送出的內容):去除客套話/重複貼出的程式碼,並在常規步驟上跳過深度「思考」。詳見 [輸出 token 減少](#輸出-token-減少刪減模型寫回的內容)。
- **可逆(CCR)** — 原始內容會被快取,可依需求取回

## 運作原理(30 秒)

```
 你的 agent / app
   (Claude Code、Cursor、Codex、LangChain、Agno、Strands、你自己的程式碼……)
        │   prompts · 工具輸出 · 日誌 · RAG 結果 · 檔案
        ▼
    ┌────────────────────────────────────────────────────┐
    │  Headroom   (在本機執行——你的資料留在這裡)         │
    │  ────────────────────────────────────────────────  │
    │  CacheAligner  →  ContentRouter  →  CCR            │
    │                    ├─ SmartCrusher   (JSON)        │
    │                    ├─ CodeCompressor (AST)         │
    │                    └─ Kompress-v2-base (文字, HF)  │
    │                                                    │
    │  跨 agent 記憶  ·  headroom learn  ·  MCP          │
    └────────────────────────────────────────────────────┘
        │   壓縮後的 prompt  +  retrieval 工具
        ▼
 LLM 提供商  (Anthropic · OpenAI · Bedrock · …)
```

- **ContentRouter** — 偵測內容類型,選擇合適的壓縮器
- **SmartCrusher / CodeCompressor / Kompress-v2-base** — 壓縮 JSON、AST 或散文文字
- **CacheAligner** - 偵測並警告可能破壞供應商 KV cache 前綴的易變內容;絕不改寫 prompt
- **CCR** — 在本機儲存原始內容;LLM 若需要可呼叫 `headroom_retrieve`

→ [架構說明](https://headroom-docs.vercel.app/docs/architecture) · [CCR 可逆壓縮](https://headroom-docs.vercel.app/docs/ccr) · [Kompress-v2-base 模型卡](https://huggingface.co/chopratejas/kompress-v2-base)

## Get started(60 秒)

```bash
# 1 — 安裝
uv tool install --python 3.13 "headroom-ai[all]"  # 以獨立虛擬環境安裝 CLI 為全域工具
pip install "headroom-ai[all]"                    # Python — 提供 `headroom` CLI
npm install headroom-ai                           # TypeScript SDK 專用 — 不含 `headroom` CLI

# 2 — 選擇你的模式(以下 `headroom` 指令來自 uv 或 pip 安裝)
headroom deploy                         # 一鍵本機部署 + agent 設定
headroom wrap claude                    # 包裝一個 coding agent
headroom proxy --port 8787              # 免改程式碼的 proxy
# 或者: from headroom import compress    # 直接內嵌 library

# 3 — 驗證設定並查看節省效果
headroom doctor                         # 健康檢查 — 確認路由運作正常
headroom perf
headroom dashboard                      # 即時節省儀表板(需先啟動 proxy)
```

建議每次都以 wrapped agent session 啟動 headroom,以確保完成所有必要設定。當包裝一個 coding agent 時,headroom 會啟動本機 proxy、安裝 **Serena** 以支援語意化程式碼導覽,並啟動一個透過 headroom 代理請求的 coding agent session。

Serena 是以 **user scope** 註冊的(以 Claude Code 為例,寫入 `~/.claude.json`),因此在你執行 `headroom unwrap` 之前,會持續在你的其他專案中可用。若要完全略過它,請以 `--code-memory none` 進行 wrap。

`headroom` CLI **僅**透過 PyPI 套件發佈。npm 上的 `headroom-ai` 是 TypeScript SDK——是你 import 使用的 library(`import { compress } from 'headroom-ai'`),不是 CLI,因此不提供 `headroom` 指令。

細部安裝選項:`[proxy]`、`[mcp]`、`[ml]`、`[code]`、`[memory]`、`[vector]`(選用的 HNSW 後端——需要 C++ 工具鏈,不包含在 `[all]` 中)、`[relevance]`、`[image]`、`[agno]`、`[langchain]`、`[evals]`、`[pytorch-mps]`(Apple GPU 記憶體 embedder offload——設定 `HEADROOM_EMBEDDER_RUNTIME=pytorch_mps`)。需要 **Python 3.10+**。

### Codex / 全域安裝

如果 Codex 或其他 MCP client 無法可靠地繼承 shell `PATH`,請將 Headroom 安裝為持久化的 uv tool,並指向絕對路徑的執行檔:

```bash
uv tool install "headroom-ai[all]"
command -v headroom
```

接著在 MCP 設定中使用取得的路徑:

```toml
[mcp_servers.headroom]
command = "/absolute/path/from/command-v/headroom"
args = ["mcp", "serve"]
```

只有當 client 啟動時的 `PATH` 已包含 uv tool 目錄時,`command = "headroom"` 才會生效。

## Proof(成效證明)

**真實 agent 工作負載的節省效果:**

| 工作負載                       | 之前   | 之後   | 節省      |
|-------------------------------|-------:|-------:|--------:|
| 程式碼搜尋(100 筆結果)         | 17,765 |  1,408 | **92%** |
| SRE 事故除錯                   | 65,694 |  5,118 | **92%** |
| GitHub issue 分流               | 54,174 | 14,761 | **73%** |
| 程式碼庫探索                    | 78,502 | 41,254 | **47%** |

**在標準基準測試上準確度不變:**

| Benchmark  | 類別     | N   | 基準值   | Headroom | 差異        |
|------------|----------|----:|---------:|---------:|------------|
| GSM8K      | 數學     | 100 |    0.870 |    0.870 | **±0.000** |
| TruthfulQA | 事實性   | 100 |    0.530 |    0.560 | **+0.030** |
| SQuAD v2   | QA       | 100 |        — |  **97%** | 19% 壓縮率 |
| BFCL       | 工具呼叫 | 100 |        — |  **97%** | 32% 壓縮率 |

重現方式:`python -m headroom.evals suite --tier 1` · [完整基準測試與方法論](https://headroom-docs.vercel.app/docs/benchmarks)

## 輸出 token 減少(刪減模型寫回的內容)

以上內容都是縮減你**送出**的 prompt。但你也要為模型**寫回**的每一個 token 付費——在 Opus 級別的模型上,輸出成本是輸入的 5 倍。
其中很多輸出都是浪費:「好的,讓我來……」這類開場白、重新印出你剛給它看過的程式碼,以及在讀檔案這類例行步驟上做過深的「思考」。

Headroom 也能從 proxy 層面刪減這些內容,而且不需要你更動任何程式碼:

- **語氣簡潔化(Verbosity steering)** — 在系統 prompt 結尾附加一段簡短的「請簡潔、不要重述上下文」提示(因此你的 prompt cache 仍然會命中)。
- **推理力度路由(Effort routing)** — 當某個 turn 只是模型在工具結果(讀檔、通過測試)之後恢復執行時,會調低模型的思考力度。新問題與錯誤則維持完整力度。

同時適用於 Anthropic `/v1/messages` **與** OpenAI 相容端點
(`/v1/chat/completions`、`/v1/responses`)。Effort routing 在 OpenAI 上使用
`reasoning_effort`,在 Anthropic 上使用 `thinking.budget_tokens` /
`output_config.effort`——兩條路徑遵守同樣的「只會調降」不變性,以及同樣的
`output_shaper:*` 標籤詞彙。

啟用方式:

```bash
export HEADROOM_OUTPUT_SHAPER=1     # 預設關閉
headroom proxy --port 8787
```

> **已經在跑一個 proxy?** 這些開關會在每個請求時**即時**讀取,
> 所以若 `headroom wrap` **重用**了現有 proxy(而非重新啟動),
> 它不會看到你之後才 export 的值——因為它的環境變數是在啟動時擷取的快照。
> 現在 `headroom wrap` 會透過 loopback 的 `POST /admin/runtime-env`
> 將目前設定即時同步給正在執行的 proxy,因此立即生效,**不需要重啟**
> (沒有 cold start、不會丟失請求、不會清空快取)。請在 `wrap` **之前**設定好這些值。
> 在共用的 proxy 上,這些覆寫是全域性的——以最後一次明確設定為準。

**學習最適合你的簡潔程度。** 人們通常不會直接「說」自己想要多簡潔的回答——而是「用行為表現出來」(打斷過長的回覆,或在讀完之前就先跳到下一步)。`headroom learn --verbosity` 會讀取你過去的 session,自動找出合適的等級:

```bash
headroom learn --verbosity            # 預覽結果(dry run)
headroom learn --verbosity --apply    # 儲存設定;proxy 從此開始套用
```

**查看你節省了多少輸出 tokens。** 輸出節省是**反事實(counterfactual)**的——我們永遠看不到模型「原本會」寫出什麼——因此 Headroom 回報的是誠實的**估計值加上信賴區間**,絕不是憑空捏造的數字:

```bash
headroom output-savings
# Reduction: 31.7%  (95% CI 27.7% … 35.7%)   [estimated]
```

想要**實測**數字而非估計值?可將 10% 的對話保留為未套用的對照組:`export HEADROOM_OUTPUT_HOLDOUT=0.1`。儀表板會在輸入壓縮旁邊顯示一張 **Output Tokens Saved** 卡片,標示 `measured`(實測)或 `estimated`(估計)及其信賴區間。

→ 完整說明(含量測方法論):[輸出 token 減少](https://headroom-docs.vercel.app/docs/savings)

<a href="https://www.star-history.com/?repos=chopratejas%2Fheadroom&type=date&legend=top-left">
 <picture>
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=chopratejas/headroom&type=date&legend=top-left" />
 </picture>
</a>

## Agent 相容性矩陣

| Agent        | `headroom wrap` | 備註                              |
|--------------|:---------------:|----------------------------------|
| Claude Code  | ✅              | `--memory` · `--code-graph` · `--1m` · `--tool-search` |
| Codex        | ✅              | 與 Claude 共用記憶               |
| Grok CLI     | ✅              | 透過 `GROK_MODELS_BASE_URL` 路由 |
| Cursor       | 手動設定        | 啟動 proxy 並印出供 Cursor 設定用的 base URL |
| Aider        | ✅              | 啟動 proxy + 直接啟動            |
| Copilot CLI  | ✅              | 啟動 proxy + 直接啟動            |
| VS Code Copilot | ✅           | 透明代理;保留原本選擇的模型     |
| OpenClaw     | ✅              | 以 ContextEngine plugin 形式安裝 |
| OpenCode     | ✅              | 注入設定 · 啟動 proxy + 直接啟動 |
| Cline        | ✅              | 啟動 proxy + 注入設定            |
| Continue     | ✅              | 啟動 proxy + 注入設定            |
| Goose        | ✅              | 啟動 proxy + 直接啟動            |
| OpenHands    | ✅              | 啟動 proxy + 直接啟動            |
| Mistral Vibe | ✅              | 啟動 proxy + 直接啟動            |
| Oh My Pi     | ✅              | 注入設定 · 啟動 proxy + 直接啟動 |
| Cortex Code  | 僅 library      | 60–65% 節省(library 模式;不支援 `wrap`) |
| Kimi CLI     | ✅              | 轉發 OAuth bearer——登入一次即可 |
| ZCode        | ✅              | 啟動 proxy 並印出供 ZCode 設定用的 base URL |

任何 OpenAI 相容的 client 都能透過 `headroom proxy` 使用。原生支援 MCP:`headroom mcp install`。
以 `headroom unwrap <tool>` 復原持久化的 wrap(支援:`claude`、`copilot`、`codex`、`grok`、`kimi`、`omp`、`opencode`、`openclaw`、`zcode`)。
Registry 作者可直接使用 repo 根目錄的權威版本 [`server.json`](server.json),不必從文字說明重建 `headroom mcp serve` 的規格。

### GitHub Copilot CLI 訂閱模式

Headroom 可以將 GitHub Copilot CLI 訂閱流量透過本機 proxy 轉發:

```bash
headroom copilot-auth login
headroom wrap copilot --subscription -- --model gpt-4o
```

這讓 Headroom 能攔截 OpenAI 相容的 Copilot CLI 請求,在轉發到 GitHub Copilot 的託管 API 之前套用相同的 proxy 壓縮流程。此 wrapper 會將 Headroom 可重複使用的 GitHub OAuth token 換成 Copilot 的短效 API token,並在啟動時印出上游端點 `COPILOT_PROVIDER_API_URL=...`。

`headroom copilot-auth login` 會儲存專屬於 Headroom 的 Copilot OAuth token。
這樣可避免依賴那些能讀取 Copilot 帳號 metadata、但可能被 Copilot 的
token 交換端點拒絕的通用 GitHub 或 Copilot CLI token。

若使用 GitHub Enterprise Server 或自訂網域的 Copilot 部署,請在啟動前設定以下其中一項:

```bash
export GITHUB_COPILOT_ENTERPRISE_DOMAIN=ghe.example.com
# 或
export GITHUB_COPILOT_ENTERPRISE_URL=https://ghe.example.com
```

兩個變數都支援。若兩者皆設定,
`GITHUB_COPILOT_ENTERPRISE_URL` 優先。

對於像 `github.com/enterprises/your-enterprise` 這類 GitHub.com Enterprise Cloud
網址,請不要設定 enterprise-domain 覆寫。Headroom 會使用 GitHub 標準的
token 交換端點,以及登入帳號所公告的 Copilot API 端點。

平台支援說明:macOS 透過 Copilot CLI Keychain 儲存的 auth 重用已完成 smoke test。Windows Credential Manager、Linux Secret Service / `secret-tool`,以及 Docker/CI 的 token 注入路徑已實作或規劃為 auth-discovery 路徑,但仍需要在真實 OS 上完成驗證才能視為完全可信。對於 Docker 與 CI,建議直接傳入明確的 `GITHUB_COPILOT_TOKEN` 或 `GITHUB_COPILOT_GITHUB_TOKEN`,而非依賴主機 keychain 存取。

### Visual Studio Code 中的 GitHub Copilot

Headroom 會透明地覆寫 Copilot 的 API proxy 端點,因此 VS
Code 一般的模型選擇器仍然是權威來源。GPT-5.5、GPT-5.6 Luna/Sol/Terra、Claude
Sonnet/Opus 及其他 Copilot 模型會保留原本的模型 ID,流量則會經過本機壓縮 proxy。Headroom 不會修改 VS Code 或
更改 Codex 設定:

```bash
headroom copilot-auth login
headroom wrap vscode
```

保持指令持續執行,並正常使用 Copilot。Headroom 只在 proxy process 中保存短效的
上游 Copilot token。
詳見[跨平台 VS Code Copilot 指南](https://headroom-docs.vercel.app/docs/vscode-copilot)
了解路徑、憑證流程、遠端開發注意事項、復原步驟與疑難排解。

### Visual Studio Code 中的 Claude Code

官方 Claude Code extension 內嵌了 Claude Code,並讀取與 CLI 相同的使用者
設定。請先安裝 Headroom 的 proxy 相依套件,然後在你打算於 VS Code
開啟的專案中執行 wrapper:

```bash
pip install "headroom-ai[proxy]"
headroom wrap vscode-claude
```

第一次執行後,請重新載入 VS Code 視窗。在使用 Claude Code 面板期間,
請保持 wrapper 終端機持續執行;可查看啟動時印出的儀表板或
proxy log 來檢視請求與節省情況。
Headroom 會保留你的 Anthropic 認證與所選模型。

按下 `Ctrl+C` 停止 proxy。下次要使用 Claude
Code 前請重新執行同一指令,或完全還原設定前的狀態:

```bash
headroom unwrap vscode-claude
```

詳見
[VS Code Claude Code 指南](https://headroom-docs.vercel.app/docs/vscode-claude-code)
了解驗證方式、設定路徑、自訂 profile、遠端開發與疑難排解。

## 何時該用 · 何時該跳過

**非常適合你,如果你……**
- 每天都在跑 AI coding agent,想要在不改程式碼的前提下節省成本
- 跨多個 agent 工作,想要共用記憶
- 需要可逆壓縮——原始內容可在設定的 TTL 內透過 CCR 取回

**可以跳過,如果你……**
- 只用單一供應商的原生 compaction,不需要跨 agent 記憶
- 在無法執行本機 process 的沙箱環境中工作

<details>
<summary><b>整合方式 — 把 Headroom 接進任何技術堆疊</b></summary>

| 你的環境                | 接入方式                                                          |
|------------------------|------------------------------------------------------------------|
| 任何 Python 應用程式    | `compress(messages, model=…)`                                    |
| 任何 TypeScript 應用程式 | `await compress(messages, { model })`                            |
| Anthropic / OpenAI SDK | `withHeadroom(new Anthropic())` · `withHeadroom(new OpenAI())`   |
| Vercel AI SDK          | `wrapLanguageModel({ model, middleware: headroomMiddleware() })` |
| LiteLLM                | `litellm.callbacks = [HeadroomCallback()]`                       |
| LangChain              | `HeadroomChatModel(your_llm)`                                    |
| Agno                   | `HeadroomAgnoModel(your_model)`                                  |
| Strands                | [Strands 指南](https://headroom-docs.vercel.app/docs/strands)   |
| ASGI 應用程式          | `app.add_middleware(CompressionMiddleware)`                      |
| Multi-agent            | `SharedContext().put / .get`                                     |
| MCP clients            | `headroom mcp install`                                           |

</details>

<details>
<summary><b>內部組成</b></summary>

- **SmartCrusher** — 通用 JSON 壓縮:dict 陣列、巢狀物件、混合型別。
- **CodeCompressor** — 支援 Python、JS/TS、Go、Rust、Java、C/C++、Perl 的 AST 感知壓縮。
- **Kompress-v2-base** — 我們的 HuggingFace 模型,以 agentic trace 訓練而成。
- **圖片壓縮** — 透過訓練過的 ML router,減少 40–90%。
- **CacheAligner** - 偵測並警告可能破壞供應商 KV cache 前綴的易變內容;絕不改寫 prompt。
- **Live-zone 壓縮** — 只壓縮新產生的位元組(最新的工具輸出、最新一輪對話);凍結的前綴保持位元組完全一致,不會破壞供應商快取。歷史紀錄永遠不會被丟棄。
- **CCR** — 可逆壓縮;LLM 可依需求取回原始內容。
- **跨 agent 記憶** — 共用儲存體、agent 來源追蹤、自動去重。
- **SharedContext** — 在多 agent 工作流程中傳遞壓縮後的上下文。
- **`headroom learn`** — 針對 Claude、Codex、Gemini 的、以 plugin 為基礎的失敗案例挖掘。

</details>

<details>
<summary><b>Pipeline 內部細節</b></summary>

Headroom 在 `compress()`、SDK 與 proxy 之間公開同一套穩定的請求生命週期:

`Setup` → `Pre-Start` → `Post-Start` → `Input Received` → `Input Cached` → `Input Routed` → `Input Compressed` → `Input Remembered` → `Pre-Send` → `Post-Send` → `Response Received`

- **Transforms** 負責實際運算:CacheAligner → ContentRouter → SmartCrusher / CodeCompressor / Kompress-base(僅限 live-zone;IntelligentContext 與 RollingWindow 已在 PR-B1 中退役)。
- **Pipeline extensions** 透過 `on_pipeline_event(...)` 觀察或客製化生命週期階段。
- **Compression hooks** 與正式生命週期並列,是另一個 extension 接入點。
- **Proxy extensions** 仍是 server/app 整合的接入點,用於 ASGI middleware、路由與啟動 policy。

供應商及工具相關行為都放在 `headroom/providers/` 之下,讓核心編排(orchestration)保持專注於生命週期、排序與 policy。

- **CLI/工具切片**:`headroom/providers/claude`、`copilot`、`codex`、`grok`、`openclaw`
- **Provider runtime 切片**:`headroom/providers/claude`、`gemini`,以及 `headroom/providers/registry.py` 中共用的 backend/runtime 派送
- **核心檔案維持以編排為主**:`wrap.py`、`client.py`、`cli/proxy.py`、`proxy/server.py` 負責委派供應商特定的環境變數處理、API 目標正規化、backend 選擇與傳輸派送。

</details>

## Headroom for teams

Headroom OSS 是為**個人開發者**打造的:在你的筆電上執行 `headroom proxy` 或 `headroom wrap`,幾分鐘內就能開始省 tokens——免費、local-first,資料永遠不離開你的機器。

在**整個工程團隊**中執行則是完全不同層級的工作:需要一個共用、常駐的部署、集中式設定與版本推送、全組織範圍的節省儀表板、SSO 與存取控制、air-gapped / VPC 安裝,以及出問題時可以聯絡的人。這正是我們協助企業處理的部分——可自架加上支援服務,或完全代管。

**如果你的團隊正在為 LLM tokens 花真金白銀**——不論是 Claude Code、Codex、Cursor,或是在 CI 中運行的 agent——**而且你希望這些節省效果能覆蓋整個團隊,而不只是一台筆電:**

→ 寄信到 **[hello@headroomlabs.ai](mailto:hello@headroomlabs.ai)**,告訴我們你的技術堆疊與每月大致的 LLM 花費,我們會協助你在整個組織中導入 Headroom。

本 repo 中的一切都會維持開源(Apache 2.0)。代管服務只是為了那些希望有人幫忙部署、支援並擴展 Headroom 的團隊而存在。

## 安裝

```bash
uv tool install --python 3.13 "headroom-ai[all]"  # CLI,獨立的 app 環境
pip install "headroom-ai[all]"                    # Python,包含所有功能——含 `headroom` CLI
npm install headroom-ai                           # TypeScript SDK(僅 library——不含 `headroom` CLI)
docker pull ghcr.io/chopratejas/headroom:latest
```

細部安裝選項:`[proxy]`、`[mcp]`、`[ml]`(Kompress-v2-base)、`[code]`、`[memory]`、`[vector]`(選用的 HNSW 後端——需要 C++ 工具鏈,不包含在 `[all]` 中)、`[relevance]`、`[image]`、`[agno]`、`[langchain]`、`[evals]`、`[pytorch-mps]`(Apple GPU 記憶體 embedder offload——設定 `HEADROOM_EMBEDDER_RUNTIME=pytorch_mps`)。需要 **Python 3.10+**。

> **注意**:`[all]` 涵蓋核心堆疊,但不包含框架轉接器(adapter)。請另外安裝:`pip install "headroom-ai[langchain]"`(也支援 `[agno]`、`[strands]`、`[anyllm]`、`[bedrock]`)。

使用 `uv` 安裝 `headroom` CLI?建議使用 `uv tool install`,讓指令位於獨立的 app 環境中。在 macOS 上,若你預設的 `python3` 版本比目前 wheel 支援的版本新,請加上 `--python 3.13`:

```bash
brew install python@3.13  # 如果尚未安裝 Python 3.13
uv tool install --python 3.13 "headroom-ai[all]"
uv tool update-shell      # 如果 ~/.local/bin 尚未加入 PATH
headroom --version
```

對於像 Codex 這類無法繼承你互動式 shell `PATH` 的 MCP client,請設定 `command -v headroom` 回傳的絕對執行檔路徑:

```toml
[mcp_servers.headroom]
command = "/Users/you/.local/bin/headroom"
args = ["mcp", "serve"]
```

目前的原生 wheel 支援 macOS Apple Silicon 與 Linux。在 Intel macOS 上,請在原生 wheel 支援上線前改用 Docker 原生安裝。

使用 `pipx`?請明確選擇支援的直譯器版本:

```bash
pipx install --python python3.13 "headroom-ai[all]"
```

> **如果你想省錢,請選 3.13。** 儀表板的 *Proxy $ Saved* 卡片是用 [LiteLLM](https://github.com/BerriAI/litellm) 為壓縮結果計價,而 LiteLLM 無法安裝在 Python 3.14+ 上。在 3.14 上 token 節省數字仍會正常追蹤,但金額欄位會固定顯示 `$0.00`。如果你已經在 3.14 上安裝,請以 `pipx reinstall headroom-ai --python python3.13` 切換版本並重啟 proxy。

→ [安裝指南](https://headroom-docs.vercel.app/docs/installation) — Docker tags、常駐服務、PowerShell、devcontainers。

> **CPU 需求(x86/x86_64):** 以 ONNX 為基礎的功能——Magika 內容
> 偵測與 embedding relevance——使用預先編譯的 ONNX Runtime,需要
> **AVX2**。在沒有 AVX2 的 x86 主機上(部分 Docker/QEMU 環境與較舊的雲端
> VM),Headroom 會自動退回非 ONNX 路徑(BM25 relevance、
> 啟發式偵測)而不是直接崩潰。`arm64`/Apple Silicon 不需要 AVX2。

### 更新

```bash
headroom update          # 自動偵測 pip / pipx / uv tool 並就地升級
headroom update --check  # 回報最新版本但不進行升級
headroom update --pre    # 包含 pre-release 版本
```

`headroom update` 會判斷 Headroom 是如何安裝的(pip/venv、`pip --user`、
pipx、uv tool),並在 macOS、Linux 與 Windows 上執行對應的升級流程。
對於 git checkout、editable 安裝、Docker 映像檔,以及外部管理的
系統 Python(PEP 668),它會印出正確的手動步驟,而不是猜測。

proxy 也會在啟動時顯示一行「有可用更新」的提示。它每天最多向
PyPI 查詢一次,在背景執行且不會阻塞。可用 `HEADROOM_UPDATE_CHECK=off`
關閉此功能(在 `--stateless` 模式與 CI 中也會自動略過)。

### 企業內部 / SSL 檢測環境

如果 `pip install "headroom-ai[all]"` 因 `CERTIFICATE_VERIFY_FAILED`
(`unable to get local issuer certificate`)而失敗,代表你的網路使用了 **SSL 檢測**——一個呈現企業自簽 CA 的 MITM
proxy。build backend(`maturin`)會透過你的 TLS 堆疊不信任的連線下載 `rustup`。**請先安裝 Rust**,讓 build 不需要再抓取它:

```bash
# macOS / Linux
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh && rustup default stable
# Windows
winget install Rustlang.Rustup && rustup default stable
```

重啟你的 shell,然後執行 `pip install "headroom-ai[all]"`。若有預先建置好的 wheel,可完全避開 Rust
build:`pip install --only-binary headroom-ai headroom-ai`。目前針對
Windows(`win_amd64`)、Linux(`x86_64` / `aarch64`)與 macOS
(Apple Silicon 與 Intel)都有發佈預建 wheel,因此在這些平台上安裝時完全不需要
本機 Rust 工具鏈——上面的 Rust-first 流程只適用於沒有對應 wheel 時的、與平台無關的 sdist 備援方案。

有兩項 runtime 資源會透過 TLS 下載;若被封鎖,請透過
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `CURL_CA_BUNDLE` 信任你的企業 CA:

- **`cdn.pyke.io`** — Rust core 使用的 ONNX Runtime。也可以改用
  `ORT_STRATEGY=system` 與 `ORT_LIB_LOCATION=/path/to/onnxruntime` 預先提供。
- **`huggingface.co`** — `kompress-base` 壓縮模型。可預先下載後以
  `HF_HUB_OFFLINE=1` 執行,或設定 `HF_ENDPOINT` 指向受信任的鏡像站。

若以停用壓縮功能的方式執行(純 gateway 模式),則兩項資源都不需要。

#### Intel macOS(x86_64-apple-darwin):沒有預建的 ONNX Runtime 二進位檔(#941)

`ort-sys` 並未針對 Intel macOS 發佈預建的 ONNX Runtime 二進位檔,因此即便在沒有企業 proxy 的環境中,原始碼 build 預設也會失敗。上面同樣的
`ORT_STRATEGY=system` 機制可以解決此問題——改為指向
系統上已安裝的 ONNX Runtime:

```bash
brew install onnxruntime
ORT_STRATEGY=system \
ORT_LIB_LOCATION="$(brew --prefix onnxruntime)/lib" \
ORT_PREFER_DYNAMIC_LINK=1 \
  pip install "headroom-ai[all]"

# ORT 在 runtime 也是透過 dlopen 載入:
export ORT_DYLIB_PATH="$(brew --prefix onnxruntime)/lib/libonnxruntime.dylib"
```

`ORT_LIB_LOCATION` 必須指向 `lib/`(而不是單純的 prefix 目錄),且
`ORT_PREFER_DYNAMIC_LINK=1` 是必要的,否則即使設定了 `ORT_STRATEGY=system`
仍會嘗試靜態連結,而 Homebrew 的套件並未提供靜態連結版本。

#### 「CA cert 的 Basic Constraints 未標記為 critical」(Python 3.13+ 嚴格模式)

這是與上面**不同**的失敗情況。若 TLS 失敗並顯示:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
Basic Constraints of CA cert not marked critical
```

那麼企業 CA *已經*被找到並信任——把它加進 CA bundle 不會改變任何事。
Python 3.13 + OpenSSL 3.x 預設啟用 `VERIFY_X509_STRICT`,這會強制執行 RFC 5280
§4.2.1.9:CA 憑證的 `basicConstraints` 必須標記為 *critical*。像 Zscaler 這類檢測用的根憑證會設定
`CA:TRUE` 但沒有加上 critical 位元,因此該憑證鏈會被拒絕。

設定 **`HEADROOM_TLS_STRICT=0`**,即可從 Headroom 控制的每個 TLS context 中
清除*僅有*嚴格模式的旗標——包含 proxy 的 httpx 上游 client **以及**
用於模型下載的 urllib3/`huggingface_hub` 路徑。憑證鏈驗證、簽章、
有效期與 hostname 檢查仍會全部保留;這比完全停用驗證要嚴格得多。

```bash
HEADROOM_TLS_STRICT=0 headroom proxy --port 8787
```

Rust core 的 ONNX 下載(`cdn.pyke.io`)使用另一套獨立的 TLS 堆疊(rustls / 作業系統信任
存放區),不受 `HEADROOM_TLS_STRICT` 影響。在 Windows 上,企業根憑證必須位於
**machine** 憑證存放區(瀏覽器在那裡已經信任它);或者預先提供
ONNX Runtime,搭配 `ORT_STRATEGY=system` + `ORT_LIB_LOCATION=/path/to/onnxruntime` 以完全跳過
下載步驟。

## headroom learn

<p align="center">
  <img src="headroom_learn.gif" alt="headroom learn in action" width="720">
</p>

`headroom learn` — 挖掘失敗的 session,將修正寫入 `CLAUDE.local.md`(預設,已加入 gitignore;團隊共用檔案請用 `--target CLAUDE.md`)/ `AGENTS.md` / `GEMINI.md`。

## 文件

| 從這裡開始                                                                    | 深入閱讀                                                                            |
|-------------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| [Quickstart](https://headroom-docs.vercel.app/docs/quickstart)                | [架構說明](https://headroom-docs.vercel.app/docs/architecture)                     |
| [Proxy](https://headroom-docs.vercel.app/docs/proxy)                          | [壓縮運作原理](https://headroom-docs.vercel.app/docs/how-compression-works)        |
| [MCP tools](https://headroom-docs.vercel.app/docs/mcp)                        | [CCR — 可逆壓縮](https://headroom-docs.vercel.app/docs/ccr)                        |
| [記憶功能](https://headroom-docs.vercel.app/docs/memory)                      | [Cache 最佳化](https://headroom-docs.vercel.app/docs/cache-optimization)           |
| [失敗學習機制](https://headroom-docs.vercel.app/docs/failure-learning)        | [基準測試](https://headroom-docs.vercel.app/docs/benchmarks)                       |
| [設定](https://headroom-docs.vercel.app/docs/configuration)                   | [限制](https://headroom-docs.vercel.app/docs/limitations)                          |
| [持久化安裝](https://headroom-docs.vercel.app/docs/persistent-installs)(`headroom init` / `headroom install apply`) | [節省數據分析](https://headroom-docs.vercel.app/docs/savings)(`headroom savings` / `headroom perf` / `headroom doctor`) |

## 比較

Headroom **在本機**執行,涵蓋**每一種**內容類型,支援每個主流框架,並且**可逆**。

|                                                                              | 涵蓋範圍                                        | 部署方式                            | 本機 | 可逆 |
|------------------------------------------------------------------------------|------------------------------------------------|------------------------------------|:-----:|:----------:|
| **Headroom**                                                                 | 所有上下文 — 工具、RAG、日誌、檔案、歷史 | Proxy · library · middleware · MCP | 是   | 是        |
| [Compresr](https://compresr.ai)、[Token Co.](https://thetokencompany.ai)    | 送到他們 API 的文字內容                        | 託管 API 呼叫                       | 否    | 否        |
| OpenAI Compaction                                                            | 對話歷史                                        | 供應商原生                          | 否    | 否        |

> **技術堆疊與整合。** Headroom 是**proxy**——這是我們打造並提供的核心產品,無論上游是什麼系統,都能壓縮流經它的所有內容。我們推薦搭配的工具是**[Serena](https://github.com/oraios/serena)**(包裝 agent 時預設會安裝)提供語意化程式碼導覽——若想要更精簡的模型輸出,也可以搭配 **Ponytail**。其餘一切都由你自行決定:你可以自由接上自己的工具——code-memory MCP、Graphify、Caveman,或任何 MCP server——Headroom 會在這些工具的下游繼續進行壓縮。

## Contributing(貢獻)

```bash
git clone https://github.com/chopratejas/headroom.git && cd headroom
uv sync --extra dev && uv run pytest
```

`.devcontainer/` 中提供 Devcontainer(預設版 + 含 Qdrant 與 Neo4j 的 `memory-stack` 版本)。詳見 [CONTRIBUTING.md](CONTRIBUTING.md)。

## Community(社群)

- **[Discord](https://discord.gg/yRmaUNpsPJ)** — 提問、回饋、經驗分享。
- **[Kompress-v2-base on HuggingFace](https://huggingface.co/chopratejas/kompress-v2-base)** — 我們文字壓縮功能背後的模型。

### 社群專案

- **[Claude Code status-line indicator](https://github.com/Ship-Wright/headroom-plugin)** — 一款在 status line 中即時顯示 Headroom 使用狀況的 Claude Code plugin:在 `headroom_compress` 觸發前保持閒置,觸發後顯示累計節省的 token 總數。

## License(授權)

Apache 2.0 — 詳見 [LICENSE](LICENSE)。
