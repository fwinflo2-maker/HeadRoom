"""Proxy server CLI commands."""

import logging
import os
import sys
import warnings
from typing import Any

import click

from headroom import paths as _paths
from headroom.providers.registry import resolve_api_overrides
from headroom.proxy.modes import PROXY_MODE_TOKEN, normalize_proxy_mode

from .main import main

# ---------------------------------------------------------------------------
# Startup log suppression.
#
# sentence_transformers makes HEAD/GET requests to HuggingFace Hub on every
# worker startup to validate the model manifest.  Each request produces an
# INFO-level httpx record and a WARNING from huggingface_hub about a missing
# HF_TOKEN.  With 8 workers this generates ~50 noisy lines per startup.
#
# Placing the suppression here (module-level in the first CLI module imported)
# ensures it is in place before sentence_transformers, huggingface_hub, or
# httpx are initialised by any downstream import or worker fork.
#
# The env vars silence the warnings.warn() path ("unauthenticated requests"
# message) which bypasses the logging system entirely.
# ---------------------------------------------------------------------------

# Env-var knobs are read by huggingface_hub before its logger hierarchy forms.
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# Corporate TLS-inspection support (issue #1308). When HEADROOM_TLS_STRICT=0,
# strip OpenSSL's RFC 5280 strict CA-constraint check from urllib3's context
# builder *before* huggingface_hub / requests import and cache it — otherwise
# model downloads (huggingface.co) fail with "Basic Constraints of CA cert not
# marked critical" behind Zscaler/Netskope on Python 3.13+. The proxy's own
# httpx upstream client is handled separately in proxy/server.py via
# build_httpx_verify(). No-op unless the toggle is set.
try:  # pragma: no cover - exercised via integration, not unit-importable cheaply
    from headroom.proxy.ssl_context import apply_global_tls_relaxation as _apply_tls_relax

    _apply_tls_relax()
except Exception:  # never let TLS relaxation wiring break startup
    pass

# Logger-level suppression: httpx HEAD/GET manifest checks + HF advisory msgs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

# warnings.warn() path: huggingface_hub emits UserWarning for missing tokens.
warnings.filterwarnings("ignore", message=".*unauthenticated.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*HF_TOKEN.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*huggingface.*token.*", category=UserWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")

# ---------------------------------------------------------------------------

_CONTEXT_TOOL_ENV = "HEADROOM_CONTEXT_TOOL"
_CONTEXT_TOOL_RTK = "rtk"
_CONTEXT_TOOL_LEAN_CTX = "lean-ctx"
_VALID_CONTEXT_TOOLS = {_CONTEXT_TOOL_RTK, _CONTEXT_TOOL_LEAN_CTX}


def _get_env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _get_env_bool_optional(name: str) -> bool | None:
    if name not in os.environ:
        return None
    return _get_env_bool(name, False)


def _get_env_int_optional(name: str) -> int | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    try:
        return int(val)
    except ValueError:
        raise click.ClickException(f"{name} must be an integer, got {val!r}") from None


def _get_env_float_optional(name: str) -> float | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except ValueError:
        raise click.ClickException(f"{name} must be a number, got {val!r}") from None


def _selected_context_tool() -> str:
    raw = os.environ.get(_CONTEXT_TOOL_ENV, "").strip().lower().replace("_", "-")
    if not raw:
        return _CONTEXT_TOOL_RTK
    if raw == "leanctx":
        raw = _CONTEXT_TOOL_LEAN_CTX
    if raw not in _VALID_CONTEXT_TOOLS:
        raise click.ClickException(
            f"{_CONTEXT_TOOL_ENV} must be one of: {', '.join(sorted(_VALID_CONTEXT_TOOLS))}"
        )
    return raw


@main.command()
@click.option(
    "--port",
    "-p",
    default=8787,
    type=click.IntRange(1, 65535),
    envvar="HEADROOM_PORT",
    help="Proxy port (default: 8787, env: HEADROOM_PORT)",
)
@click.option("--no-open", is_flag=True, help="Print the URL instead of opening a browser")
def dashboard(port: int, no_open: bool) -> None:
    """Open the Headroom savings dashboard in your browser.

    Requires a running proxy (start one with `headroom proxy` or `headroom wrap ...`).
    """
    import webbrowser

    url = f"http://127.0.0.1:{port}/dashboard"
    click.echo(f"  Dashboard: {url}")
    if not no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless/no browser: URL already printed
            pass


@main.command()
@click.option(
    "--host",
    default="127.0.0.1",
    envvar="HEADROOM_HOST",
    help="Host to bind to (default: 127.0.0.1, env: HEADROOM_HOST)",
)
@click.option(
    "--port",
    "-p",
    default=8787,
    type=click.IntRange(1, 65535),
    envvar="HEADROOM_PORT",
    help="Port to bind to (default: 8787, env: HEADROOM_PORT)",
)
@click.option(
    "--workers",
    default=1,
    type=click.IntRange(min=1),
    envvar="HEADROOM_WORKERS",
    help="Number of Uvicorn worker processes (default: 1, env: HEADROOM_WORKERS)",
)
@click.option(
    "--limit-concurrency",
    default=1000,
    type=click.IntRange(min=1),
    envvar="HEADROOM_LIMIT_CONCURRENCY",
    help=(
        "Maximum concurrent connections before Uvicorn returns 503 "
        "(default: 1000, env: HEADROOM_LIMIT_CONCURRENCY)"
    ),
)
@click.option(
    "--max-connections",
    default=500,
    type=click.IntRange(min=1),
    envvar="HEADROOM_MAX_CONNECTIONS",
    help="Maximum upstream HTTP connections (default: 500, env: HEADROOM_MAX_CONNECTIONS)",
)
@click.option(
    "--max-keepalive",
    "max_keepalive_connections",
    default=100,
    type=click.IntRange(min=0),
    envvar="HEADROOM_MAX_KEEPALIVE",
    help="Maximum upstream keep-alive connections (default: 100, env: HEADROOM_MAX_KEEPALIVE)",
)
@click.option(
    "--http2/--no-http2",
    "http2",
    default=True,
    envvar="HEADROOM_HTTP2",
    help=(
        "Use HTTP/2 to upstream providers (default: on, env: HEADROOM_HTTP2). "
        "Disable to force HTTP/1.1, which avoids shared-connection TLS corruption "
        "(SSLV3_ALERT_BAD_RECORD_MAC) when many concurrent streams are cancelled."
    ),
)
@click.option(
    "--keepalive-expiry",
    "keepalive_expiry",
    default=90.0,
    type=click.FloatRange(min=0),
    envvar="HEADROOM_KEEPALIVE_EXPIRY",
    help="Seconds an idle upstream keep-alive connection is kept open (default: 90, env: HEADROOM_KEEPALIVE_EXPIRY)",
)
@click.option(
    "--mode",
    default=None,
    metavar="[token|cache]",
    type=click.Choice(
        # Canonical modes first; legacy aliases follow for backward compatibility.
        # `metavar` above hides the alias clutter from --help; users see "[token|cache]"
        # while internal callers passing "token_mode"/"cost_savings"/etc. still validate.
        [
            "token",
            "cache",
            "token_mode",
            "cache_mode",
            "token_savings",
            "cost_savings",
            "token_headroom",
        ],
        case_sensitive=False,
    ),
    help=(
        "Optimization mode (default: token).\n"
        "  token  — prioritize compression; prior turns may be rewritten for max savings.\n"
        "  cache  — freeze prior turns to maximise provider prefix-cache hit rate.\n"
        "Legacy aliases (token_mode, token_savings, token_headroom, cache_mode, "
        "cost_savings) are still accepted. Env: HEADROOM_MODE."
    ),
)
@click.option(
    "--target-ratio",
    type=float,
    default=None,
    show_default=True,
    envvar="HEADROOM_TARGET_RATIO",
    help=(
        "Override Kompress keep-ratio for text (prose/code) compression — lower is "
        "more aggressive (e.g. 0.4 keeps ~40% of tokens). Unset (default): let "
        "Kompress decide via its own importance threshold (conservative). "
        "Env: HEADROOM_TARGET_RATIO."
    ),
)
@click.option(
    "--intercept-tool-results",
    is_flag=True,
    help=(
        "Opt in to tool_result interceptors (ast-grep Read outliner, etc.). "
        "Off by default while this feature ships."
    ),
)
@click.option("--no-optimize", is_flag=True, help="Disable optimization (passthrough mode)")
@click.option("--no-cache", is_flag=True, help="Disable semantic caching")
@click.option("--no-rate-limit", is_flag=True, help="Disable rate limiting")
@click.option(
    "--protect-tool-results",
    default=None,
    envvar="HEADROOM_PROTECT_TOOL_RESULTS",
    help=(
        "Comma-separated tool names whose results are never lossy-compressed, "
        "merged with the built-in defaults (e.g. Bash,WebFetch). "
        "Env: HEADROOM_PROTECT_TOOL_RESULTS."
    ),
)
@click.option(
    "--rpm",
    default=None,
    type=click.IntRange(min=1),
    envvar="HEADROOM_RPM",
    help="Max requests per minute. Env: HEADROOM_RPM. Default: 60.",
)
@click.option(
    "--tpm",
    default=None,
    type=click.IntRange(min=1),
    envvar="HEADROOM_TPM",
    help="Max tokens per minute. Env: HEADROOM_TPM. Default: 100000.",
)
@click.option(
    "--no-ccr-inject-tool",
    is_flag=True,
    envvar="HEADROOM_NO_CCR_INJECT_TOOL",
    help=(
        "Don't inject the CCR headroom_retrieve tool. Run compression-only — "
        "for streaming / non-MCP clients that can't resolve the retrieve tool "
        "and would otherwise error on it. Env: HEADROOM_NO_CCR_INJECT_TOOL."
    ),
)
@click.option(
    "--no-ccr-marker",
    is_flag=True,
    envvar="HEADROOM_NO_CCR_MARKER",
    help=("Don't add CCR retrieval markers to compressed content. Env: HEADROOM_NO_CCR_MARKER."),
)
@click.option(
    "--no-ccr-proactive-expansion",
    is_flag=True,
    envvar="HEADROOM_NO_CCR_PROACTIVE_EXPANSION",
    help=(
        "Disable proactive expansion of previously compressed content. "
        "Env: HEADROOM_NO_CCR_PROACTIVE_EXPANSION."
    ),
)
@click.option(
    "--proxy-extension",
    "proxy_extension",
    multiple=True,
    envvar="HEADROOM_PROXY_EXTENSIONS",
    help=(
        "Enable a registered proxy extension by entry-point name (opt-in). "
        "Repeat the flag or pass a comma-separated list. Use '*' to enable "
        "every discovered extension. Env: HEADROOM_PROXY_EXTENSIONS."
    ),
)
@click.option(
    "--no-subscription-tracking",
    is_flag=True,
    envvar="HEADROOM_NO_SUBSCRIPTION_TRACKING",
    help=(
        "Disable the Anthropic Claude Code subscription usage poller "
        "(GET /api/oauth/usage). Env: HEADROOM_NO_SUBSCRIPTION_TRACKING."
    ),
)
@click.option(
    "--subscription-poll-interval",
    type=click.IntRange(min=1, max=3600),
    default=None,
    envvar="HEADROOM_SUBSCRIPTION_POLL_INTERVAL",
    help=(
        "Seconds between Anthropic subscription usage polls (1–3600, default 300). "
        "Lower values give fresher /stats but risk 429s from Anthropic. "
        "Env: HEADROOM_SUBSCRIPTION_POLL_INTERVAL."
    ),
)
@click.option(
    "--retry-max-attempts",
    type=click.IntRange(min=1, max=10),
    default=None,
    envvar="HEADROOM_RETRY_MAX_ATTEMPTS",
    help=(
        "Maximum upstream retry attempts for connect/read/5xx failures (1–10, default: 3). "
        "Env: HEADROOM_RETRY_MAX_ATTEMPTS."
    ),
)
@click.option(
    "--request-timeout-seconds",
    type=int,
    default=None,
    envvar="HEADROOM_REQUEST_TIMEOUT",
    help=(
        "Request timeout in seconds (default: 300). "
        "Useful for slow providers (eg local). "
        "Env: HEADROOM_REQUEST_TIMEOUT."
    ),
)
@click.option(
    "--connect-timeout-seconds",
    type=click.IntRange(min=1, max=300),
    default=None,
    envvar="HEADROOM_CONNECT_TIMEOUT_SECONDS",
    help=(
        "Upstream connection timeout in seconds (1–300, default: 10). "
        "Env: HEADROOM_CONNECT_TIMEOUT_SECONDS."
    ),
)
@click.option(
    "--anthropic-buffered-request-timeout-seconds",
    type=click.IntRange(min=1),
    default=None,
    envvar="HEADROOM_ANTHROPIC_BUFFERED_REQUEST_TIMEOUT_SECONDS",
    help=(
        "Buffered Anthropic read timeout in seconds for non-streaming "
        "message and batch paths (default: 600). "
        "Env: HEADROOM_ANTHROPIC_BUFFERED_REQUEST_TIMEOUT_SECONDS."
    ),
)
@click.option(
    "--anthropic-pre-upstream-concurrency",
    type=int,
    default=None,
    envvar="HEADROOM_ANTHROPIC_PRE_UPSTREAM_CONCURRENCY",
    help=(
        "Cap the number of Anthropic HTTP requests that may run pre-upstream work "
        "(request parse / deep-copy / first compression stage / memory context / upstream connect) "
        "concurrently. Prevents cold-start replay storms from starving /livez and new Codex WS opens. "
        "Default: max(2, min(8, os.cpu_count() or 4)). "
        "Set to 0 or negative to disable (unbounded). "
        "Env: HEADROOM_ANTHROPIC_PRE_UPSTREAM_CONCURRENCY."
    ),
)
@click.option(
    "--anthropic-pre-upstream-acquire-timeout-seconds",
    type=float,
    default=None,
    envvar="HEADROOM_ANTHROPIC_PRE_UPSTREAM_ACQUIRE_TIMEOUT_SECONDS",
    help=(
        "Fail-fast timeout for waiting on the Anthropic pre-upstream semaphore "
        "before returning 503 + Retry-After. "
        "Default: 15.0 seconds. "
        "Env: HEADROOM_ANTHROPIC_PRE_UPSTREAM_ACQUIRE_TIMEOUT_SECONDS."
    ),
)
@click.option(
    "--anthropic-pre-upstream-memory-context-timeout-seconds",
    type=float,
    default=None,
    envvar="HEADROOM_ANTHROPIC_PRE_UPSTREAM_MEMORY_CONTEXT_TIMEOUT_SECONDS",
    help=(
        "Fail-open timeout for Anthropic memory-context lookup while the request "
        "still holds a pre-upstream slot. "
        "Default: 2.0 seconds. "
        "Env: HEADROOM_ANTHROPIC_PRE_UPSTREAM_MEMORY_CONTEXT_TIMEOUT_SECONDS."
    ),
)
@click.option(
    "--log-file",
    default=None,
    envvar="HEADROOM_LOG_FILE",
    help=(
        "Path to write request/response logs as JSONL. "
        "Each line is a JSON object with fields: timestamp, request_id, model, "
        "tokens_before, tokens_after, latency_ms, etc. "
        "Disabled in --stateless mode. Env: HEADROOM_LOG_FILE."
    ),
)
@click.option(
    "--log-messages",
    is_flag=True,
    envvar="HEADROOM_LOG_MESSAGES",
    help=(
        "Enable full message logging: request/response content is stored in the log file "
        "and served on the live feed endpoint. WARNING: may log sensitive data. "
        "Env: HEADROOM_LOG_MESSAGES."
    ),
)
@click.option(
    "--codex-wire-debug",
    is_flag=True,
    help="Enable local Codex wire snapshots and matching proxy.log frame traces.",
)
@click.option(
    "--codex-wire-debug-dir",
    default=None,
    help=(
        "Directory for Codex wire snapshots (default: "
        "~/.headroom/logs/codex_wire or workspace .headroom/logs/codex_wire)."
    ),
)
@click.option(
    "--budget",
    type=click.FloatRange(min=0.0),
    default=None,
    envvar="HEADROOM_BUDGET",
    help=(
        "Budget limit in USD per --budget-period. Requests are rejected with 429 "
        "once the limit is reached. Env: HEADROOM_BUDGET."
    ),
)
@click.option(
    "--budget-period",
    type=click.Choice(["hourly", "daily", "monthly"]),
    default="daily",
    envvar="HEADROOM_BUDGET_PERIOD",
    help=(
        "Period the --budget limit applies to. Hourly resets on a rolling hour, "
        "daily at local midnight, monthly on the 1st. Default: daily. "
        "Env: HEADROOM_BUDGET_PERIOD."
    ),
)
# Code-aware compression (AST-based, requires `pip install headroom-ai[code]`).
# Pair of flags so users can override the env-var default in either direction.
# We resolve HEADROOM_CODE_AWARE_ENABLED in the body (not via Click's envvar=),
# because Click's envvar handling for paired bool flags is brittle in older
# Click versions.
@click.option(
    "--code-aware/--no-code-aware",
    "code_aware_flag",
    default=None,
    help=(
        "Enable/disable AST-based code compression. Requires the optional "
        "tree-sitter dependency: pip install headroom-ai[code]. "
        "Default: disabled. Env: HEADROOM_CODE_AWARE_ENABLED=1 to enable."
    ),
)
@click.option(
    "--disable-kompress",
    is_flag=True,
    envvar="HEADROOM_DISABLE_KOMPRESS",
    help=(
        "Disable Kompress ML compression while keeping structural compression enabled. "
        "Env: HEADROOM_DISABLE_KOMPRESS=1."
    ),
)
@click.option(
    "--disable-kompress-fallback",
    is_flag=True,
    envvar="HEADROOM_DISABLE_KOMPRESS_FALLBACK",
    help=(
        "With --disable-kompress, route fall-through content to PASSTHROUGH instead of "
        "the default KOMPRESS fallback (restores legacy --disable-kompress behaviour). "
        "Env: HEADROOM_DISABLE_KOMPRESS_FALLBACK=1."
    ),
)
@click.option(
    "--disable-kompress-anthropic/--enable-kompress-anthropic",
    "disable_kompress_anthropic",
    default=None,
    envvar="HEADROOM_DISABLE_KOMPRESS_ANTHROPIC",
    help=(
        "Disable (or --enable-) Kompress for the Anthropic pipeline only, overriding "
        "--disable-kompress. Env: HEADROOM_DISABLE_KOMPRESS_ANTHROPIC=1."
    ),
)
@click.option(
    "--disable-kompress-openai/--enable-kompress-openai",
    "disable_kompress_openai",
    default=None,
    envvar="HEADROOM_DISABLE_KOMPRESS_OPENAI",
    help=(
        "Disable (or --enable-) Kompress for the OpenAI/Codex pipeline only, overriding "
        "--disable-kompress. Env: HEADROOM_DISABLE_KOMPRESS_OPENAI=1."
    ),
)
# Code graph: indexes project + watches files for live reindex via codebase-memory-mcp.
# Only useful when the proxy is launched from a project root — it indexes the
# current working directory.
@click.option(
    "--code-graph",
    is_flag=True,
    help=(
        "Enable code graph intelligence: indexes the current working directory "
        "and watches files for live reindex via codebase-memory-mcp. Only useful "
        "when the proxy is launched from a project root."
    ),
)
# Read lifecycle (ON by default: compresses stale/superseded Read outputs)
@click.option(
    "--no-read-lifecycle",
    is_flag=True,
    help="Disable Read lifecycle management (stale/superseded Read compression)",
)
# Read maturation (Mechanism B) — experimental, OFF by default
@click.option(
    "--read-maturation",
    is_flag=True,
    envvar="HEADROOM_READ_MATURATION",
    help=(
        "EXPERIMENTAL: activity-based read maturation — hold fresh Reads "
        "out of the provider prefix cache and compress them once their "
        "file quiesces (env: HEADROOM_READ_MATURATION=1)"
    ),
)
@click.option(
    "--read-maturation-quiesce-turns",
    type=click.IntRange(min=1),
    default=5,
    show_default=True,
    envvar="HEADROOM_READ_MATURATION_QUIESCE_TURNS",
    help="Read maturation: mature a held Read once its file is quiet this many assistant turns.",
)
@click.option(
    "--read-maturation-max-hold-turns",
    type=click.IntRange(min=1),
    default=25,
    show_default=True,
    envvar="HEADROOM_READ_MATURATION_MAX_HOLD_TURNS",
    help="Read maturation: force-mature a Read held this many turns even if its file stays active.",
)
@click.option(
    "--read-maturation-min-size-bytes",
    type=click.IntRange(min=0),
    default=2048,
    show_default=True,
    envvar="HEADROOM_READ_MATURATION_MIN_SIZE_BYTES",
    help="Read maturation: only hold/mature Read outputs at least this many bytes.",
)
# Memory System (Multi-Provider Support)
@click.option(
    "--memory",
    is_flag=True,
    help=(
        "Enable persistent memory. Auto-detects provider and uses appropriate tool format. "
        "By default (--memory-storage=project) each workspace gets its own DB so memories "
        "from unrelated projects can never bleed in (GH #462). Override scoping with "
        "x-headroom-user-id and/or x-headroom-project-id / x-headroom-cwd request headers."
    ),
)
@click.option(
    "--memory-db-path",
    default="",
    envvar="HEADROOM_MEMORY_DB_PATH",
    help=(
        "Path to the legacy single-file memory DB (used in --memory-storage=global, "
        "and as the seed for the project-mode storage root). "
        "Default: {cwd}/.headroom/memory.db. Env: HEADROOM_MEMORY_DB_PATH."
    ),
)
@click.option(
    "--memory-storage",
    type=click.Choice(["project", "user", "global"], case_sensitive=False),
    default="project",
    show_default=True,
    help=(
        "Memory partitioning strategy. project (default): one SQLite DB per resolved "
        "workspace under <db_path_dir>/memories/projects/<basename>-<hash>/memory.db — "
        "no cross-project bleed. user: one DB per x-headroom-user-id. global: a single "
        "shared DB (pre-fix behaviour; --memory-db-path file is reused so existing "
        "memories remain reachable)."
    ),
)
@click.option(
    "--memory-project-root",
    default="",
    envvar="HEADROOM_MEMORY_PROJECT_ROOT",
    help=(
        "Override the project root used for --memory-storage=project. Useful when the "
        "client doesn't put a cwd in the system prompt or you want to force a specific "
        "workspace. Takes effect after the x-headroom-project-id and x-headroom-cwd "
        "headers. Env: HEADROOM_MEMORY_PROJECT_ROOT."
    ),
)
@click.option(
    "--no-memory-tools",
    is_flag=True,
    envvar="HEADROOM_NO_MEMORY_TOOLS",
    help=(
        "Disable automatic injection of memory_save/memory_search tools into requests. "
        "Env: HEADROOM_NO_MEMORY_TOOLS."
    ),
)
@click.option(
    "--no-memory-context",
    is_flag=True,
    envvar="HEADROOM_NO_MEMORY_CONTEXT",
    help=(
        "Disable automatic injection of relevant past memories into the system prompt. "
        "Env: HEADROOM_NO_MEMORY_CONTEXT."
    ),
)
@click.option(
    "--memory-top-k",
    type=click.IntRange(min=1, max=100),
    default=10,
    envvar="HEADROOM_MEMORY_TOP_K",
    help=(
        "Number of semantically-relevant memories to inject as context (1–100, default: 10). "
        "Env: HEADROOM_MEMORY_TOP_K."
    ),
)
@click.option(
    "--memory-qdrant-url",
    default=None,
    help=(
        "Full Qdrant URL for the qdrant-neo4j backend "
        "(e.g. https://xyz.cloud.qdrant.io:6333). When set, takes precedence over "
        "--memory-qdrant-host/--memory-qdrant-port. "
        "Also reads HEADROOM_QDRANT_URL."
    ),
)
@click.option(
    "--memory-qdrant-host",
    default=None,
    help=(
        "Qdrant host for the qdrant-neo4j backend "
        "(default: localhost, also reads HEADROOM_QDRANT_HOST)"
    ),
)
@click.option(
    "--memory-qdrant-port",
    type=click.IntRange(1, 65535),
    default=None,
    help=(
        "Qdrant port for the qdrant-neo4j backend (default: 6333, also reads HEADROOM_QDRANT_PORT)"
    ),
)
@click.option(
    "--memory-qdrant-api-key",
    default=None,
    help=("API key for hosted Qdrant (e.g. Qdrant Cloud). Also reads HEADROOM_QDRANT_API_KEY."),
)
# Traffic Learning (live pattern extraction from proxy traffic)
@click.option(
    "--learn",
    is_flag=True,
    help="Enable live traffic learning: extract error→recovery patterns, environment facts, "
    "and user preferences from proxy traffic. Implies --memory. "
    "Learned patterns are saved to agent-native memory files (MEMORY.md, .cursor/rules, AGENTS.md).",
)
@click.option(
    "--no-learn",
    is_flag=True,
    help="Explicitly disable traffic learning even when --memory is set.",
)
@click.option(
    "--min-evidence",
    type=click.IntRange(min=1),
    default=None,
    envvar="HEADROOM_MIN_EVIDENCE",
    help=(
        "Minimum number of times a pattern must be observed before it is "
        "persisted to memory. Higher values reduce one-shot noise at the "
        "cost of slower learning. Default: 5. (env: HEADROOM_MIN_EVIDENCE)"
    ),
)
# Backend configuration
@click.option(
    "--backend",
    default="anthropic",
    envvar="HEADROOM_BACKEND",
    help=(
        "API backend: 'anthropic' (direct), 'bedrock' (AWS), 'openrouter' (OpenRouter), "
        "'anyllm' (any-llm), or 'litellm-<provider>' (e.g., litellm-vertex). "
        "Env: HEADROOM_BACKEND."
    ),
)
@click.option(
    "--anyllm-provider",
    default="openai",
    envvar="HEADROOM_ANYLLM_PROVIDER",
    help=(
        "Provider for any-llm backend: openai, mistral, groq, ollama, etc. (default: openai). "
        "Env: HEADROOM_ANYLLM_PROVIDER."
    ),
)
@click.option(
    "--anthropic-api-url",
    default=None,
    help="Custom Anthropic API URL for passthrough endpoints (env: ANTHROPIC_TARGET_API_URL)",
)
@click.option(
    "--openai-api-url",
    default=None,
    help="Custom OpenAI API URL for passthrough endpoints (env: OPENAI_TARGET_API_URL)",
)
@click.option(
    "--gemini-api-url",
    default=None,
    help="Custom Gemini API URL for passthrough endpoints (env: GEMINI_TARGET_API_URL)",
)
@click.option(
    "--cloudcode-api-url",
    default=None,
    help="Custom Cloud Code Assist API URL for compatibility endpoints (env: CLOUDCODE_TARGET_API_URL)",
)
@click.option(
    "--vertex-api-url",
    default=None,
    help=("Custom Vertex AI regional API URL for publisher endpoints (env: VERTEX_TARGET_API_URL)"),
)
@click.option(
    "--region",
    default="us-west-2",
    envvar="HEADROOM_REGION",
    help="Cloud region for Bedrock/Vertex/etc (default: us-west-2). Env: HEADROOM_REGION.",
)
@click.option(
    "--bedrock-region",
    default=None,
    help="(deprecated, use --region) AWS region for Bedrock",
)
@click.option(
    "--bedrock-profile",
    default=None,
    help="AWS profile name for Bedrock (default: use default credentials)",
)
@click.option(
    "--bedrock-api-url",
    default=None,
    help=(
        "Custom Bedrock InvokeModel upstream for the /model/{id}/invoke "
        "passthrough routes. Point at a re-signing gateway (LiteLLM, "
        "LocalStack), NOT raw AWS — rewriting the body breaks SigV4. "
        "(env: BEDROCK_TARGET_API_URL)"
    ),
)
@click.option(
    "--telemetry",
    is_flag=True,
    help="Opt in to anonymous usage telemetry — off by default (env: HEADROOM_TELEMETRY=on)",
)
@click.option(
    "--no-telemetry",
    is_flag=True,
    help="Force anonymous usage telemetry off (already the default; env: HEADROOM_TELEMETRY=off)",
)
@click.option(
    "--stateless",
    is_flag=True,
    help="Disable all filesystem writes — run purely in-memory. "
    "For containerized / read-only / load-balanced deployments. "
    "(env: HEADROOM_STATELESS=true)",
)
@click.option(
    "--embedding-server/--no-embedding-server",
    default=False,
    help="Run a dedicated embedding server sidecar (Option E). "
    "Shares a single ONNX embedder + HNSW index across all worker processes, "
    "saving ~600 MB RSS. Default: disabled (opt-in for testing). "
    "(env: HEADROOM_EMBEDDING_SERVER=true)",
)
@click.option(
    "--embedding-server-socket",
    default=None,
    help="Unix socket path for the embedding server sidecar. "
    "Default: /tmp/headroom-embed-{port}.sock. "
    "(env: HEADROOM_EMBEDDING_SERVER_SOCKET)",
)
@click.pass_context
def proxy(
    ctx: click.Context,
    mode: str | None,
    target_ratio: float | None,
    host: str,
    port: int,
    workers: int,
    limit_concurrency: int,
    max_connections: int,
    max_keepalive_connections: int,
    keepalive_expiry: float,
    http2: bool,
    intercept_tool_results: bool,
    no_optimize: bool,
    no_cache: bool,
    no_rate_limit: bool,
    protect_tool_results: str | None,
    rpm: int | None,
    tpm: int | None,
    no_ccr_inject_tool: bool,
    no_ccr_marker: bool,
    no_ccr_proactive_expansion: bool,
    proxy_extension: tuple[str, ...],
    no_subscription_tracking: bool,
    subscription_poll_interval: int | None,
    retry_max_attempts: int | None,
    request_timeout_seconds: int | None,
    connect_timeout_seconds: int | None,
    anthropic_buffered_request_timeout_seconds: int | None,
    anthropic_pre_upstream_concurrency: int | None,
    anthropic_pre_upstream_acquire_timeout_seconds: float | None,
    anthropic_pre_upstream_memory_context_timeout_seconds: float | None,
    log_file: str | None,
    log_messages: bool,
    codex_wire_debug: bool,
    codex_wire_debug_dir: str | None,
    budget: float | None,
    budget_period: str,
    code_aware_flag: bool | None,
    disable_kompress: bool,
    disable_kompress_fallback: bool,
    disable_kompress_anthropic: bool | None,
    disable_kompress_openai: bool | None,
    code_graph: bool,
    no_read_lifecycle: bool,
    read_maturation: bool,
    read_maturation_quiesce_turns: int,
    read_maturation_max_hold_turns: int,
    read_maturation_min_size_bytes: int,
    memory: bool,
    memory_db_path: str,
    memory_storage: str,
    memory_project_root: str,
    no_memory_tools: bool,
    no_memory_context: bool,
    memory_top_k: int,
    memory_qdrant_url: str | None,
    memory_qdrant_host: str | None,
    memory_qdrant_port: int | None,
    memory_qdrant_api_key: str | None,
    learn: bool,
    no_learn: bool,
    min_evidence: int | None,
    backend: str,
    anyllm_provider: str,
    anthropic_api_url: str | None,
    openai_api_url: str | None,
    gemini_api_url: str | None,
    cloudcode_api_url: str | None,
    vertex_api_url: str | None,
    region: str,
    bedrock_region: str | None,
    bedrock_profile: str | None,
    bedrock_api_url: str | None,
    telemetry: bool,
    no_telemetry: bool,
    stateless: bool,
    embedding_server: bool,
    embedding_server_socket: str | None,
) -> None:
    """Start the optimization proxy server.

    \b
    Examples:
        headroom proxy                    Start proxy on port 8787
        headroom proxy --port 8080        Start proxy on port 8080
        headroom proxy --no-optimize      Passthrough mode (no optimization)

    \b
    Usage with Claude Code:
        ANTHROPIC_BASE_URL=http://localhost:8787 claude

    \b
    Usage with OpenAI-compatible clients:
        OPENAI_BASE_URL=http://localhost:8787/v1 your-app
    """
    # Phase H1: the Python proxy server is retired. This function now locates
    # the headroom-proxy Rust binary and execs it, inheriting all env vars
    # that Click has already validated. The binary reads its configuration
    # exclusively from env vars (HEADROOM_PROXY_*); this shim maps the legacy
    # Click flags to those vars for backward compatibility.
    import pathlib
    import shutil

    # Telemetry opt-in/out (env vars read by the Rust binary)
    if telemetry and no_telemetry:
        click.secho(
            "Warning: both --telemetry and --no-telemetry specified; --no-telemetry wins.",
            fg="yellow",
            err=True,
        )
    if telemetry:
        os.environ["HEADROOM_TELEMETRY"] = "on"
    if no_telemetry:
        os.environ["HEADROOM_TELEMETRY"] = "off"

    # Stateless: suppress TOIN filesystem persistence
    _is_stateless = stateless or os.environ.get("HEADROOM_STATELESS", "").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )
    if _is_stateless:
        os.environ["HEADROOM_TOIN_BACKEND"] = "none"

    # Wire debug
    if codex_wire_debug or codex_wire_debug_dir:
        os.environ["HEADROOM_CODEX_WIRE_DEBUG"] = "1"
        os.environ["HEADROOM_CODEX_WIRE_DEBUG_DIR"] = codex_wire_debug_dir or str(
            _paths.codex_wire_debug_dir()
        )

    # Warn on retired Python-proxy-only flags so callers know they are ignored.
    _retired: list[str] = []
    if memory:
        _retired.append("--memory")
    if no_cache:
        _retired.append("--no-cache")
    if no_rate_limit:
        _retired.append("--no-rate-limit")
    if rpm is not None:
        _retired.append("--rpm")
    if tpm is not None:
        _retired.append("--tpm")
    if intercept_tool_results:
        _retired.append("--intercept-tool-results")
    if workers != 1:
        _retired.append("--workers")
    if embedding_server:
        _retired.append("--embedding-server")
    if _retired:
        click.secho(
            f"Warning: {', '.join(_retired)}: "
            "these flags are not supported by the Rust proxy and are ignored. "
            "See docs/operations/python-to-rust-migration.md",
            fg="yellow",
            err=True,
        )

    # Resolve upstream URL from provider flags (Anthropic is the default)
    provider_api_overrides = resolve_api_overrides(
        anthropic_api_url=anthropic_api_url,
        openai_api_url=openai_api_url,
        gemini_api_url=gemini_api_url,
        cloudcode_api_url=cloudcode_api_url,
        vertex_api_url=vertex_api_url,
        environ=os.environ,
    )

    # Map Click flags → Rust env vars (setdefault so explicit env vars from the
    # operator's shell always win over CLI-derived values).
    os.environ.setdefault("HEADROOM_PROXY_LISTEN", f"{host}:{port}")
    _upstream = provider_api_overrides.anthropic or "https://api.anthropic.com"
    os.environ.setdefault("HEADROOM_PROXY_UPSTREAM", _upstream)

    if no_optimize:
        os.environ["HEADROOM_PROXY_COMPRESSION"] = "false"
    else:
        os.environ["HEADROOM_PROXY_COMPRESSION"] = "true"
        _effective_mode = normalize_proxy_mode(
            mode or os.environ.get("HEADROOM_MODE") or PROXY_MODE_TOKEN
        )
        os.environ.setdefault("HEADROOM_PROXY_COMPRESSION_MODE", _effective_mode)

    if bedrock_region or region:
        os.environ.setdefault("HEADROOM_PROXY_BEDROCK_REGION", bedrock_region or region)
    if bedrock_profile:
        os.environ.setdefault("HEADROOM_PROXY_AWS_PROFILE", bedrock_profile)
    if target_ratio is not None:
        os.environ.setdefault("HEADROOM_TARGET_RATIO", str(target_ratio))
    if request_timeout_seconds is not None:
        os.environ.setdefault(
            "HEADROOM_PROXY_UPSTREAM_TIMEOUT", f"{request_timeout_seconds}s"
        )

    # Locate the headroom-proxy Rust binary.
    # 1. On PATH (installed via pip wheel or cargo install).
    # 2. Dev worktree: <repo-root>/target/release/headroom-proxy.
    rust_binary = shutil.which("headroom-proxy")
    if not rust_binary:
        _dev = pathlib.Path(__file__).parents[3] / "target" / "release" / "headroom-proxy"
        if _dev.is_file():
            rust_binary = str(_dev)
    if not rust_binary:
        click.secho(
            "Error: headroom-proxy binary not found on PATH.\n"
            "  Installed users: pip install --upgrade headroom-ai\n"
            "  Developers:      cargo build -p headroom-proxy --release",
            fg="red",
            err=True,
        )
        raise SystemExit(1)

    # Print startup banner
    _mode_display = (
        "DISABLED (passthrough)"
        if no_optimize
        else normalize_proxy_mode(mode or os.environ.get("HEADROOM_MODE") or PROXY_MODE_TOKEN)
    )
    click.echo(f"""
\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557
\u2551                         HEADROOM PROXY                                 \u2551
\u2551           The Context Optimization Layer for LLM Applications          \u2551
\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d

  URL:          http://{host}:{port}
  Mode:         {_mode_display}
  Binary:       {rust_binary}
  Upstream:     {_upstream}
""")

    # Replace this process with the Rust binary; it inherits the env vars set above.
    try:
        os.execvp(rust_binary, [rust_binary])
    except OSError as exc:
        click.secho(f"Error: failed to exec {rust_binary!r}: {exc}", fg="red", err=True)
        raise SystemExit(1) from exc

