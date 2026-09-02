# Gemini 3.8 Flash on Google Cloud Vertex AI + Headroom Benchmark

A reproducible, real-world benchmark evaluating **Headroom context compression** with **Gemini 3.8 Flash** on **Google Cloud Vertex AI (Gemini Enterprise Agent Platform)**.

---

## 🎯 Why This Matters

When building production agents (coding assistants, SRE incident responders, data analysts, multi-agent frameworks) on Vertex AI, multi-turn tool loops cause **rapid context explosion**:

* **Container & Kubernetes logs** dump hundreds of lines of noise for a single stack trace.
* **Code search & file trees** inflate prompts with repetitive schema structures.
* **Database queries** return large tabular results where only outliers and aggregations matter.

Even with Gemini 3.8 Flash's massive context window and fast inference, bloated tool returns:
1. **Drive up inference spend** quadratically as conversation histories compound.
2. **Increase Time to First Token (TTFT)** due to large prompt processing.
3. **Dilute attention**, making needle-in-a-haystack reasoning harder.

**Headroom** acts as an intelligent, transparent proxy (or in-process SDK layer) that compresses JSON arrays, structured logs, and tables by **40–80%** while strictly preserving schema anchors, recent turns, anomalies, error traces, and ground truth accuracy.

---

## 📊 Live Benchmark Results

Tested live on **Google Cloud Vertex AI** (`global` endpoint) with **`gemini-3.8-flash`**:

| Scenario | Workload Category | Baseline Prompt | Headroom Prompt | Token Reduction | Accuracy Retention |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **SRE Incident Root Cause** | Kubernetes & Microservice Logs | 51,775 | 13,927 | **-73.1%** | 100% (✓ PASS) |
| **Security Audit & PR Review** | Code Search & Git Diffs | 6,813 | 4,624 | **-32.1%** | 100% (✓ PASS) |
| **BigQuery Table Analytics** | 500 Tabular Transaction Rows | 49,020 | 38,278 | **-21.9%** | 100% (✓ PASS) |
| **Multi-Turn RAG Synthesis** | 25 Dense Specification Chunks | 4,071 | 4,932 | **+21.1%**¹ | 100% (✓ PASS) |
| **TOTAL / AGGREGATE** | **Real-World Agent Trajectory** | **111,679** | **61,761** | **-44.7%** | **100% Preserved** |

¹ Small payloads (< 5k tokens) may see slight inflation from Headroom's CCR retrieval metadata. Compression ROI increases with payload size.

### Key Metrics Summary

* **Prompt Tokens Saved**: **49,918 tokens** (**44.7% net reduction**)
* **Inference Cost Reduction**: **41.0% savings** on Vertex AI standard tier
* **Ground Truth Accuracy**: **100% retained** across all scenarios (exact error trace, database credentials, auth bypass vector, and high-spend outliers all correctly identified)

---

## 🏗️ Architecture

```
                                          ┌───────────────────────────────────────┐
                                          │ Google Cloud Vertex AI                │
                                          │ (Gemini Enterprise Agent Platform)    │
                                          │                                       │
┌───────────────────────┐                 │  ┌─────────────────────────────────┐  │
│  google-genai Python  │                 │  │        gemini-3.8-flash         │  │
│  SDK Agent / Script   │                 │  └─────────────────────────────────┘  │
└───────────┬───────────┘                 └───────────────────▲───────────────────┘
            │                                                 │
            │  POST /v1/projects/.../publishers/...           │ Compressed
            │  (base_url = http://127.0.0.1:8787)             │ Payload
            ▼                                                 │
┌─────────────────────────────────────────────────────────────┴───────────────────┐
│ Headroom Proxy (:8787)                                                          │
│                                                                                 │
│  ┌───────────────────────┐   ┌────────────────────────┐   ┌──────────────────┐  │
│  │ ContentRouter         │──▶│ SmartCrusher           │──▶│ LogCompressor    │  │
│  │ (Format & Role Sieve) │   │ (JSON Array Compactor) │   │ (Error Anchor)   │  │
│  └───────────────────────┘   └────────────────────────┘   └──────────────────┘  │
│                                                                                 │
│  • Preserves Google ADC Bearer Auth Tokens                                      │
│  • Compresses verbose tool arrays & tables                                      │
│  • Preserves 100% of anomalies, errors, and schema anchors                      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 🚀 How to Run the Benchmark

### 1. Prerequisites

Ensure you have Google Cloud Application Default Credentials (ADC) configured:

```bash
gcloud auth application-default login
export GCP_PROJECT_ID=$(gcloud config get-value project)
```

Install required dependencies:

```bash
pip install "headroom-ai[proxy,vertex]" google-genai tabulate rich
```

### 2. Execute Benchmark

Run the full comparative suite:

```bash
python examples/vertex_gemini_benchmark/benchmark.py --model gemini-3.8-flash
```

#### CLI Options

```text
--project         GCP Project ID (defaults to $GCP_PROJECT_ID or gcloud default)
--location        Vertex AI location (default: global)
--model           Vertex model ID (default: gemini-3.8-flash)
--port            Headroom local proxy port (default: 8787)
--thinking-budget Thinking token budget in tokens (default: 0 = standard inference)
--output-json     Output file for JSON metrics (default: examples/vertex_gemini_benchmark/results.json)
--social          Print formatted social media proof point summary
```

---

## 💡 Using Headroom with Vertex AI in Your Agent Code

Connecting your `google-genai` agent to Headroom requires **one line** (`http_options`):

```python
from google import genai

# Point the standard SDK at the Headroom proxy
client = genai.Client(
    vertexai=True,
    project="your-gcp-project-id",
    location="global",
    http_options={"base_url": "http://127.0.0.1:8787"},
)

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=[
        "You are an SRE agent.",
        f"Analyze these Kubernetes logs:\n{verbose_json_logs}",
    ],
)
print(response.text)
```

---

## 📢 Social Post / Proof Point Card

```markdown
🚀 Headroom + Gemini 3.8 Flash on Google Cloud Vertex AI Benchmark

When AI agents run complex multi-turn workflows (SRE debugging, PR reviews, BigQuery analytics), tool output bloat explodes prompt token costs and degrades TTFT.

We ran reproducible end-to-end agent benchmarks comparing Direct Vertex AI vs Headroom-Proxied Vertex AI on gemini-3.8-flash:

📉 Results:
• Prompt Token Reduction: 44.7% (111,679 ➔ 61,761 tokens)
• SRE Log Scenario Reduction: 73.1% (51.7k ➔ 13.9k tokens)
• Total Cost Savings: 41.0%
• Reasoning & Fact Accuracy: 100% Preserved across all test cases
• Zero Code Changes: Point google-genai SDK http_options.base_url to the proxy.

🔗 Full benchmark suite, reproducible scenarios, and code:
https://github.com/headroomlabs-ai/headroom/tree/main/examples/vertex_gemini_benchmark
```
