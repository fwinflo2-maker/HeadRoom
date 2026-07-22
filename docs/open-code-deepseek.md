# Using Headroom with OpenCode + DeepSeek

Save 20-60% on DeepSeek API costs with Headroom's context compression proxy.

## How it works

```
OpenCode → Headroom Proxy (:8787) → DeepSeek API
              ↑ compresses input
              + shapes output
```

The proxy sits between OpenCode and DeepSeek. It compresses tool outputs, logs,
and search results before they reach the model, then shapes responses to be
concise. DeepSeek's API is OpenAI-compatible — one flag and you're running.

---

## 1. Install Headroom

```bash
pip install headroom-ai
# or via uv:
uv tool install headroom-ai
```

You get SmartCrusher (structural compression), the proxy, output shaping, and
the MCP server — everything you need.

---

## 2. Get your DeepSeek API key

Sign up at [platform.deepseek.com](https://platform.deepseek.com) and generate
an API key.

Store it somewhere safe:
```bash
export DEEPSEEK_API_KEY="sk-your-deepseek-key-here"
```

---

## 3. Start the proxy

```bash
headroom proxy \
  --port 8787 \
  --openai-api-url https://api.deepseek.com/v1
```

The proxy auto-detects `api.deepseek.com` and labels itself "DeepSeek" on the
dashboard. Verify it's running:

```bash
curl http://127.0.0.1:8787/health
# → "status": "healthy"
```

### With output shaping (optional)

Output shaping makes the model's responses shorter — fewer tokens, lower cost:

```bash
HEADROOM_OUTPUT_SHAPER=1 HEADROOM_VERBOSITY_LEVEL=2 \
headroom proxy --port 8787 --openai-api-url https://api.deepseek.com/v1
```

Verbosity levels:

| Level | Behavior |
|---|---|
| `1` | Skip preambles/postambles |
| `2` | + Don't restate code/file content already in context (**recommended**) |
| `3` | + Omit rationale unless asked |
| `4` | Maximum — fragments, zero fluff |

---

## 4. Configure OpenCode

Edit `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "headroom/deepseek-v4-pro",
  "provider": {
    "headroom": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Headroom Proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-your-deepseek-key"
      },
      "models": {
        "deepseek-v4-pro": {
          "name": "DeepSeek V4 Pro",
          "limit": { "context": 200000, "output": 16384 }
        },
        "deepseek-v4-flash": {
          "name": "DeepSeek V4 Flash (Free)",
          "limit": { "context": 200000, "output": 16384 }
        },
        "deepseek-reasoner": {
          "name": "DeepSeek R1",
          "limit": { "context": 128000, "output": 8192 }
        }
      }
    }
  },
  "mcp": {
    "headroom": {
      "type": "local",
      "command": ["headroom", "mcp", "serve"],
      "enabled": true
    }
  }
}
```

### Model comparison

| Model | Cost | Context | Best for |
|---|---|---|---|
| `deepseek-v4-pro` | Paid ($0.435/$0.87) | 200K | Complex coding, architecture, debugging |
| `deepseek-v4-flash` | Free | 200K | Daily tasks, quick edits, exploration |
| `deepseek-reasoner` (R1) | Paid | 128K | Math, logic, multi-step reasoning |

Switch models at any time with `/model` in OpenCode.

---

## 5. Start OpenCode

```bash
opencode
```

Run `/models` to confirm all three DeepSeek models appear. Select one with
`/model deepseek-v4-pro`.

---

## 6. Check savings

```bash
curl http://127.0.0.1:8787/stats | python3 -m json.tool | grep -A5 compression
```

Or open the dashboard at [http://127.0.0.1:8787/dashboard](http://127.0.0.1:8787/dashboard).

---

## Common issues

### "Authentication Fails" / Unauthorized

The `apiKey` in OpenCode's config is missing or wrong. OpenCode must send the
API key to the proxy, and the proxy forwards it to DeepSeek. Make sure
`"apiKey": "sk-..."` is set under `options` in `opencode.jsonc`.

### Models appear but requests fail

You ran `headroom wrap opencode`. That command replaces your config with Claude
and GPT models. **Do not use `headroom wrap`.** Configure OpenCode manually as
shown above, and launch OpenCode directly with `opencode`.

### "headroom" command not found

`uv tool install` puts binaries in `~/.local/bin/`. Add it to your PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Output shaping shows no savings

Output savings are measured against a learned baseline (it compares "what the
model actually emitted" vs "what it would have emitted unshaped"). After a few
sessions, run:

```bash
headroom learn --verbosity --apply
```

This builds the baseline, and `/stats` will show output savings numbers. The
shaper is active immediately — the numbers just need calibration.

---

## What's NOT in this guide

- **Claude or GPT models** — this setup uses DeepSeek exclusively
- **`headroom wrap`** — do not use it; it overrides the config
- **Kompress (ML compression)** — requires extra dependencies; SmartCrusher
  handles the majority of use cases
- **Any code changes** — headroom ships full DeepSeek support natively
  (model tables, pricing, tokenizers, domain detection)
