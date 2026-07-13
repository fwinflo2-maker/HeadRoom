//! POST `/v1/chat/completions` handler — Phase C PR-C2.
//!
//! # Why an explicit handler?
//!
//! Most paths flow through `forward_http` via the catch-all fallback;
//! the path gate in `forward_http` runs the per-provider live-zone
//! dispatcher (added to the gate by PR-C2). Spec PR-C2 still mandates
//! an explicit route handler for `/v1/chat/completions` so future
//! Phase-D wiring (Bedrock OpenAI-shape, Vertex), Phase-E auth-mode
//! gating, and per-endpoint rate-limit shaping have an obvious
//! attachment point.
//!
//! # What this handler does
//!
//! 1. Pre-buffers the request body (Bytes) so we can inspect
//!    `n`, `stream`, `messages`, `tool_choice`, `stream_options`
//!    before forwarding.
//! 2. Reconstructs a `Request<Body>` from the buffered bytes plus
//!    the original method, URI, and headers.
//! 3. Hands off to [`crate::proxy::forward_http`] — the same single
//!    forwarder that the catch-all uses. The compression gate inside
//!    `forward_http` re-classifies the path and runs
//!    [`crate::compression::compress_openai_chat_request`].
//!
//! Re-using `forward_http` keeps the SSE state-machine wiring
//! (PR-C1), header-stripping (PR-A5), `x-headroom-*` policy, and
//! request-id plumbing single-source. The alternative — duplicating
//! the forwarder body inside this handler — would diverge over time.
//!
//! # Skip / passthrough behaviours surfaced here
//!
//! - **`n > 1`** — multiple completions imply non-determinism.
//!   `compression::should_skip_compression` (called from the gate
//!   inside `forward_http`) returns `NGreaterThanOne(n)` and the
//!   gate skips dispatch entirely. The handler does not need to
//!   touch the body.
//! - **`stream: true`** — handled by the existing SSE state-machine
//!   tee in `forward_http` (PR-C1's `ChunkState`).
//! - **`tool_choice` change** — never read, never mutated.
//!   `tools[]` definitions live in the cache hot zone and the
//!   live-zone dispatcher only walks `messages[*].content`.
//! - **`stream_options.include_usage`** — same. Round-trips byte-equal
//!   as a side effect of byte-range surgery in the dispatcher.

use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, Method, Request, Uri};
use axum::response::Response;
use bytes::Bytes;
use std::net::SocketAddr;
use http::StatusCode; // Added for GlobalCheck error responses
use serde::Deserialize; // Added for JSON parsing

use crate::proxy::{forward_http, AppState};

// --- GlobalCheck Integration: Minimal Client Setup (for out-of-the-box demo) ---
// In a production application, these types would typically reside in a dedicated
// `crates/globalcheck_client` or `src/globalcheck` module, and the `GlobalCheckClient`
// instance would be initialized once and managed by the `AppState`.
// This setup is provided directly in the handler for ease of demonstration and
// "out-of-the-box" integration.

#[derive(Deserialize, Debug)]
struct ChatMessage {
    content: String,
    role: String,
}

#[derive(Deserialize, Debug)]
struct ChatCompletionRequest {
    messages: Vec<ChatMessage>,
    #[serde(default)]
    stream: bool,
}

/// Configuration for the GlobalCheck compliance service.
struct GlobalCheckConfig {
    enabled: bool,
    api_endpoint: String,
    api_key: Option<String>,
}

impl Default for GlobalCheckConfig {
    fn default() -> Self {
        GlobalCheckConfig {
            enabled: false, // Default to disabled, enable via env var or config
            api_endpoint: "http://localhost:8080/globalcheck/api/v1/compliance".to_string(), // Default local endpoint
            api_key: None,
        }
    }
}

/// A simplified GlobalCheck client for demonstration.
/// In a real scenario, this would involve an HTTP client (e.g., `reqwest`)
/// to interact with a GlobalCheck service endpoint.
struct GlobalCheckClient {
    config: GlobalCheckConfig,
    // Real client would have: http_client: reqwest::Client,
}

impl GlobalCheckClient {
    fn new(config: GlobalCheckConfig) -> Self {
        Self { config }
    }

    /// Simulates sending a chat completion request to the GlobalCheck compliance service.
    /// Returns `Ok(())` if compliant, or `Err(String)` with a violation message.
    async fn check_compliance(&self, request_body: &ChatCompletionRequest) -> Result<(), String> {
        if !self.config.enabled {
            tracing::debug!("GlobalCheck is disabled or not configured. Skipping compliance check.");
            return Ok(());
        }

        tracing::info!(
            event = "globalcheck_pre_llm_compliance_scan",
            endpoint = %self.config.api_endpoint,
            "Initiating GlobalCheck compliance scan for chat completion request."
        );

        // Iterate through messages and apply compliance rules.
        // This is a placeholder for actual GlobalCheck API calls and policy evaluation.
        for message in &request_body.messages {
            let content_lower = message.content.to_lowercase();
            if content_lower.contains("secret") || content_lower.contains("confidential") || content_lower.contains("private key") {
                tracing::warn!(
                    event = "globalcheck_violation_detected",
                    role = %message.role,
                    content_preview = %&message.content[..std::cmp::min(message.content.len(), 50)],
                    "GlobalCheck: Detected sensitive keyword in message. Blocking request."
                );
                return Err(format!("Compliance policy violation: sensitive keyword detected in message from role '{}'.", message.role));
            }
            // Add more sophisticated checks here, e.g., PII detection, regulated industry terms,
            // or a call to a remote GlobalCheck API endpoint.
        }

        // Simulate network latency if this were a real external API call.
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        tracing::info!("GlobalCheck: Request passed all compliance checks. Proceeding.");
        Ok(())
    }
}
// --- End GlobalCheck Integration Client Setup ---

/// Axum POST handler for `/v1/chat/completions`. Buffers the body,
/// stitches a fresh `Request<Body>` together, and forwards via
/// [`forward_http`]. Compression dispatch + SSE telemetry is handled
/// inside `forward_http`'s shared gate (PR-C1 + PR-C2).
pub async fn handle_chat_completions(
    State(state): State<AppState>,
    ConnectInfo(client_addr): ConnectInfo<SocketAddr>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // --- GlobalCheck Pre-LLM Compliance Enforcement ---
    // This section intercepts the request, parses the chat messages, and
    // performs a compliance check using GlobalCheck before forwarding
    // the request to the upstream LLM provider.
    let chat_req: ChatCompletionRequest = match serde_json::from_slice(&body) {
        Ok(req) => req,
        Err(e) => {
            tracing::warn!(
                event = "globalcheck_parse_body_failed",
                error = %e,
                "Failed to parse chat completion request body for GlobalCheck pre-scan. Returning 400."
            );
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .body(Body::from(format!("Invalid request body: {}", e)))
                .expect("static response");
        }
    };

    // Instantiate a GlobalCheck client (in a real app, this would be from `AppState`).
    let globalcheck_config = GlobalCheckConfig {
        enabled: std::env::var("GLOBALCHECK_ENABLED").map_or(false, |s| s.eq_ignore_ascii_case("true")),
        api_endpoint: std::env::var("GLOBALCHECK_ENDPOINT")
            .unwrap_or_else(|_| GlobalCheckConfig::default().api_endpoint),
        api_key: std::env::var("GLOBALCHECK_API_KEY").ok(),
    };
    let globalcheck_client = GlobalCheckClient::new(globalcheck_config);

    if let Err(compliance_error) = globalcheck_client.check_compliance(&chat_req).await {
        tracing::error!(
            event = "globalcheck_blocked_request",
            error = %compliance_error,
            client_addr = %client_addr,
            "GlobalCheck compliance check failed. Request blocked before LLM."
        );
        return Response::builder()
            .status(StatusCode::FORBIDDEN) // 403 Forbidden indicates policy violation
            .body(Body::from(format!("GlobalCheck Policy Violation: {}", compliance_error)))
            .expect("static response");
    }
    // --- End GlobalCheck Pre-LLM Compliance Enforcement ---

    // Reconstruct the Request<Body> shape forward_http expects.
    // Cloning the headers into a fresh builder keeps the original
    // method/uri/version intact. `axum::body::Body::from(Bytes)` is
    // a single-shot stream, which is exactly what the buffered
    // compression branch wants.
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(hs) = builder.headers_mut() {
        *hs = headers;
    }
    let req = match builder.body(Body::from(body)) {
        Ok(r) => r,
        Err(e) => {
            // Building the request out of pieces we already have
            // shouldn't fail; if it does it's an internal bug. Don't
            // silently swallow — log loudly and 500.
            tracing::error!(
                event = "handler_error",
                handler = "chat_completions",
                error = %e,
                "failed to reconstruct request from buffered body"
            );
            return Response::builder()
                .status(http::StatusCode::INTERNAL_SERVER_ERROR)
                .body(Body::from("internal handler error"))
                .expect("static response");
        }
    };

    forward_http(state, client_addr, req)
        .await
        .unwrap_or_else(|e| {
            use axum::response::IntoResponse;
            e.into_response()
        })
}
