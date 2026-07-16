//! Response-usage capture, shared by every lane.
//!
//! Recording a request's savings needs the *response's* token usage,
//! which arrives in four wire shapes:
//!
//! - JSON bodies (non-streaming Anthropic / OpenAI / Bedrock /
//!   Vertex responses),
//! - Anthropic-style SSE streams (direct, Vertex, and Bedrock's
//!   SSE-translated mode) — already accumulated by
//!   [`crate::sse::anthropic::AnthropicStreamState`],
//! - Converse-native frames whose usage keys are camelCase and carry
//!   no Anthropic `type` (the state machine drops them),
//! - Bedrock binary EventStream passthrough, where nothing parses
//!   the frames today.
//!
//! This module provides one [`PendingRecord`] (a request's
//! half-built ledger event plus the deferred-finalization contract:
//! **record only after the response is fully observed**, so
//! streaming token counts are real instead of zero) and the capture
//! tasks that feed it. Every capture path is a bounded, best-effort
//! tee off the client byte path — a stalled or hostile response can
//! delay telemetry, never the client.

use std::sync::Arc;
use std::time::Instant;

use base64::Engine as _;
use serde_json::Value;

use super::ledger::{Ledger, RequestEvent, Usage};

/// Cap for buffered non-streaming response bodies awaiting a usage
/// parse. Bigger bodies finalize with zero usage (tokens/USD from
/// compression estimates only) rather than growing memory.
const JSON_CAPTURE_CAP: usize = 2 * 1024 * 1024;

/// Queue depth for capture tees — same rationale as the SSE parser
/// queue in `proxy.rs`.
const CAPTURE_QUEUE_DEPTH: usize = 256;

/// A request's ledger event, waiting for its response to finish.
///
/// Constructed when the request is forwarded; finalized exactly once
/// when the response is fully observed (stream closed / body ended /
/// transport failed). Latency is measured here so every lane reports
/// the same thing: time from forwarding decision to response fully
/// observed.
pub struct PendingRecord {
    ledger: Arc<Ledger>,
    event: RequestEvent,
    started: Instant,
}

impl PendingRecord {
    pub fn new(ledger: Arc<Ledger>, event: RequestEvent, started: Instant) -> Self {
        Self {
            ledger,
            event,
            started,
        }
    }

    /// Mark the upstream outcome failed (non-2xx or transport error)
    /// before finalization.
    pub fn mark_failed(&mut self) {
        self.event.failed = true;
    }

    /// Supply the model id the request side knows (path parameter,
    /// or the buffered request body when compression is on).
    pub fn set_model(&mut self, model: &str) {
        self.event.set_model(model);
    }

    /// Supply the model id learned from the *response*. Recording is
    /// not gated on the compression buffer (compression defaults
    /// off), so on a pure passthrough request this is the only place
    /// the model is known. Never overrides a request-side value.
    pub fn set_model_if_unknown(&mut self, model: &str) {
        if self.event.model_is_unknown() {
            self.event.set_model(model);
        }
    }

    /// Attach the compression dispatcher's before/after token
    /// estimate and the strategies it applied.
    pub fn set_compression(&mut self, before: u64, after: u64, transforms: Vec<String>) {
        self.event.tokens_before = before;
        self.event.tokens_after = after;
        self.event.transforms = transforms;
    }

    /// Record with the captured usage. Consumes the record — each
    /// request records exactly once.
    pub fn finalize(mut self, usage: Usage) {
        self.event.usage = usage;
        self.event.latency_ms = self.started.elapsed().as_millis() as u64;
        self.ledger.record(self.event);
    }

    /// Record a failure (zero usage, no savings — the ledger's
    /// failed-path accounting).
    pub fn finalize_failed(mut self) {
        self.mark_failed();
        self.finalize(Usage::default());
    }
}

/// Which JSON body shape to expect when parsing a non-streaming
/// response for usage.
#[derive(Debug, Clone, Copy)]
pub enum ResponseShape {
    Anthropic,
    OpenAiChat,
    OpenAiResponses,
    /// Bedrock sync responses: InvokeModel bodies are Anthropic
    /// snake_case, Converse bodies are camelCase — try both.
    Bedrock,
}

/// Extract usage from a complete response body. `None` when the
/// body has no recognisable usage block (error envelopes, non-JSON).
pub fn usage_from_response_json(shape: ResponseShape, v: &Value) -> Option<Usage> {
    usage_from_usage_block(shape, v.get("usage")?)
}

/// Parse a bare `usage` object (already extracted from its
/// envelope) — the SSE state machines hold usage in this form.
pub fn usage_from_usage_block(shape: ResponseShape, usage: &Value) -> Option<Usage> {
    match shape {
        ResponseShape::Anthropic => snake_usage(usage),
        ResponseShape::OpenAiChat => openai_chat_usage(usage),
        ResponseShape::OpenAiResponses => openai_responses_usage(usage),
        ResponseShape::Bedrock => snake_usage(usage).or_else(|| camel_usage(usage)),
    }
}

/// Model id as reported by the response envelope. Anthropic,
/// OpenAI Chat/Responses, and Bedrock InvokeModel all echo `model`
/// at the top level. (Bedrock Converse doesn't — its handler
/// already knows the id from the URL path.)
pub fn model_from_response_json(v: &Value) -> Option<&str> {
    v.get("model").and_then(Value::as_str)
}

fn u64_at(v: &Value, key: &str) -> Option<u64> {
    v.get(key).and_then(Value::as_u64)
}

/// Anthropic `usage` block (also Bedrock InvokeModel for Anthropic
/// models): `input_tokens` already EXCLUDES cache reads/writes.
fn snake_usage(usage: &Value) -> Option<Usage> {
    let input = u64_at(usage, "input_tokens");
    let output = u64_at(usage, "output_tokens");
    if input.is_none() && output.is_none() {
        return None;
    }
    Some(Usage {
        input_tokens: input.unwrap_or(0),
        output_tokens: output.unwrap_or(0),
        cache_read_tokens: u64_at(usage, "cache_read_input_tokens").unwrap_or(0),
        cache_write_tokens: u64_at(usage, "cache_creation_input_tokens").unwrap_or(0),
    })
}

/// Bedrock Converse `usage` block: camelCase, and `inputTokens`
/// likewise excludes cache tokens.
fn camel_usage(usage: &Value) -> Option<Usage> {
    let input = u64_at(usage, "inputTokens");
    let output = u64_at(usage, "outputTokens");
    if input.is_none() && output.is_none() {
        return None;
    }
    Some(Usage {
        input_tokens: input.unwrap_or(0),
        output_tokens: output.unwrap_or(0),
        cache_read_tokens: u64_at(usage, "cacheReadInputTokens")
            .or_else(|| u64_at(usage, "cacheReadInputTokenCount"))
            .unwrap_or(0),
        cache_write_tokens: u64_at(usage, "cacheWriteInputTokens")
            .or_else(|| u64_at(usage, "cacheWriteInputTokenCount"))
            .unwrap_or(0),
    })
}

/// OpenAI Chat Completions: `prompt_tokens` INCLUDES cached tokens —
/// normalise to the ledger's Anthropic semantics (input = uncached).
fn openai_chat_usage(usage: &Value) -> Option<Usage> {
    let prompt = u64_at(usage, "prompt_tokens")?;
    let cached = usage
        .get("prompt_tokens_details")
        .and_then(|d| u64_at(d, "cached_tokens"))
        .unwrap_or(0)
        .min(prompt);
    Some(Usage {
        input_tokens: prompt - cached,
        output_tokens: u64_at(usage, "completion_tokens").unwrap_or(0),
        cache_read_tokens: cached,
        cache_write_tokens: 0,
    })
}

/// OpenAI Responses: `input_tokens` INCLUDES cached tokens — same
/// normalisation.
fn openai_responses_usage(usage: &Value) -> Option<Usage> {
    let input = u64_at(usage, "input_tokens")?;
    let cached = usage
        .get("input_tokens_details")
        .and_then(|d| u64_at(d, "cached_tokens"))
        .unwrap_or(0)
        .min(input);
    Some(Usage {
        input_tokens: input - cached,
        output_tokens: u64_at(usage, "output_tokens").unwrap_or(0),
        cache_read_tokens: cached,
        cache_write_tokens: 0,
    })
}

/// Usage accumulated by the shared Anthropic SSE state machine.
pub fn usage_from_anthropic_state(state: &crate::sse::anthropic::AnthropicStreamState) -> Usage {
    Usage {
        input_tokens: state.usage.input_tokens,
        output_tokens: state.usage.output_tokens,
        cache_read_tokens: state.usage.cache_read_input_tokens,
        cache_write_tokens: state.usage.cache_creation_input_tokens,
    }
}

/// Fold one streaming frame's JSON payload into a usage accumulator.
/// Handles every Bedrock/Anthropic stream shape (fields are monotone
/// within a stream, so `max` merge is safe against repeats):
///
/// - Anthropic SSE events: `message_start.message.usage`,
///   `message_delta.usage` (snake_case),
/// - Converse-native frames: bare `usage` with camelCase keys
///   (`metadata` events — no Anthropic `type` field at all),
/// - Bedrock InvokeModel EventStream chunks: `{"bytes": "<base64>"}`
///   wrapping an Anthropic SSE event (recursed after decode).
pub fn merge_stream_usage(acc: &mut Usage, payload: &Value) {
    // InvokeModel EventStream chunk: base64-wrapped inner event.
    if let Some(b64) = payload.get("bytes").and_then(Value::as_str) {
        if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(b64) {
            if let Ok(inner) = serde_json::from_slice::<Value>(&decoded) {
                merge_stream_usage(acc, &inner);
            }
        }
        return;
    }
    // message_start carries usage nested under `message`.
    if let Some(u) = payload
        .get("message")
        .and_then(|m| m.get("usage"))
        .and_then(snake_usage)
    {
        merge_max(acc, u);
    }
    // message_delta (and some terminal frames) carry a bare `usage`.
    if let Some(usage) = payload.get("usage") {
        if let Some(u) = snake_usage(usage).or_else(|| camel_usage(usage)) {
            merge_max(acc, u);
        }
    }
}

/// Take the field-wise maximum of two usage snapshots (usage fields
/// are monotone within a stream, so `max` merge is repeat-safe).
pub(crate) fn merge_max(acc: &mut Usage, u: Usage) {
    acc.input_tokens = acc.input_tokens.max(u.input_tokens);
    acc.output_tokens = acc.output_tokens.max(u.output_tokens);
    acc.cache_read_tokens = acc.cache_read_tokens.max(u.cache_read_tokens);
    acc.cache_write_tokens = acc.cache_write_tokens.max(u.cache_write_tokens);
}

/// Bounded tee → JSON-body usage capture. Returns the sender to tee
/// response chunks into; finalizes `pending` when the channel
/// closes (i.e. the response body finished or the client hung up).
pub fn spawn_json_usage_capture(
    shape: ResponseShape,
    mut pending: PendingRecord,
) -> tokio::sync::mpsc::Sender<bytes::Bytes> {
    let (tx, mut rx) = tokio::sync::mpsc::channel::<bytes::Bytes>(CAPTURE_QUEUE_DEPTH);
    tokio::spawn(async move {
        let mut buf: Vec<u8> = Vec::new();
        let mut truncated = false;
        while let Some(chunk) = rx.recv().await {
            if truncated {
                continue; // keep draining so the tee never backpressures
            }
            if buf.len() + chunk.len() > JSON_CAPTURE_CAP {
                truncated = true;
                buf.clear();
                continue;
            }
            buf.extend_from_slice(&chunk);
        }
        let mut usage = Usage::default();
        if !truncated {
            if let Ok(v) = serde_json::from_slice::<Value>(&buf) {
                if let Some(model) = model_from_response_json(&v) {
                    pending.set_model_if_unknown(model);
                }
                usage = usage_from_response_json(shape, &v).unwrap_or_default();
            }
        }
        pending.finalize(usage);
    });
    tx
}

/// Bounded tee → Bedrock binary EventStream usage capture, for the
/// passthrough mode where no other parser sees the frames. CRC
/// validation is off — this is the telemetry side; enforcement
/// happens (or not, per config) on the byte path.
pub fn spawn_eventstream_usage_capture(
    pending: PendingRecord,
) -> tokio::sync::mpsc::Sender<bytes::Bytes> {
    use crate::bedrock::eventstream::{CrcValidation, EventStreamParser};
    let (tx, mut rx) = tokio::sync::mpsc::channel::<bytes::Bytes>(CAPTURE_QUEUE_DEPTH);
    tokio::spawn(async move {
        let mut parser = EventStreamParser::new().with_crc_validation(CrcValidation::No);
        let mut usage = Usage::default();
        'outer: while let Some(chunk) = rx.recv().await {
            parser.push(&chunk);
            loop {
                match parser.next_message() {
                    Ok(Some(msg)) => {
                        if let Ok(payload) = serde_json::from_slice::<Value>(&msg.payload) {
                            merge_stream_usage(&mut usage, &payload);
                        }
                    }
                    Ok(None) => break,
                    Err(_) => {
                        // Telemetry parser out of sync — stop parsing,
                        // drain the channel, record what we have.
                        break 'outer;
                    }
                }
            }
        }
        pending.finalize(usage);
    });
    tx
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn anthropic_json_usage_parses() {
        let v = json!({"usage": {"input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}});
        let u = usage_from_response_json(ResponseShape::Anthropic, &v).unwrap();
        assert_eq!(
            u,
            Usage {
                input_tokens: 10,
                output_tokens: 5,
                cache_read_tokens: 7,
                cache_write_tokens: 3
            }
        );
    }

    #[test]
    fn openai_chat_normalises_cached_out_of_prompt() {
        let v = json!({"usage": {"prompt_tokens": 100, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 60}}});
        let u = usage_from_response_json(ResponseShape::OpenAiChat, &v).unwrap();
        assert_eq!(u.input_tokens, 40, "prompt minus cached");
        assert_eq!(u.cache_read_tokens, 60);
        assert_eq!(u.output_tokens, 20);
        // Pathological cached > prompt clamps instead of underflowing.
        let v = json!({"usage": {"prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 60}}});
        let u = usage_from_response_json(ResponseShape::OpenAiChat, &v).unwrap();
        assert_eq!(u.input_tokens, 0);
        assert_eq!(u.cache_read_tokens, 10);
    }

    #[test]
    fn openai_responses_normalises_cached_out_of_input() {
        let v = json!({"usage": {"input_tokens": 80, "output_tokens": 9,
            "input_tokens_details": {"cached_tokens": 50}}});
        let u = usage_from_response_json(ResponseShape::OpenAiResponses, &v).unwrap();
        assert_eq!(u.input_tokens, 30);
        assert_eq!(u.cache_read_tokens, 50);
    }

    #[test]
    fn bedrock_shape_accepts_snake_and_camel() {
        let snake = json!({"usage": {"input_tokens": 10, "output_tokens": 5}});
        let camel = json!({"usage": {"inputTokens": 11, "outputTokens": 6,
            "cacheReadInputTokens": 4, "cacheWriteInputTokens": 2}});
        assert_eq!(
            usage_from_response_json(ResponseShape::Bedrock, &snake)
                .unwrap()
                .input_tokens,
            10
        );
        let u = usage_from_response_json(ResponseShape::Bedrock, &camel).unwrap();
        assert_eq!(u.input_tokens, 11);
        assert_eq!(u.cache_read_tokens, 4);
        assert_eq!(u.cache_write_tokens, 2);
    }

    #[test]
    fn error_envelope_yields_none() {
        let v = json!({"error": {"type": "overloaded_error"}});
        assert!(usage_from_response_json(ResponseShape::Anthropic, &v).is_none());
        let v = json!({"usage": {"totalTokens": 5}});
        assert!(usage_from_response_json(ResponseShape::Bedrock, &v).is_none());
    }

    #[test]
    fn stream_merge_handles_all_three_wire_shapes() {
        let mut acc = Usage::default();
        // 1. Anthropic message_start (nested under message).
        merge_stream_usage(
            &mut acc,
            &json!({"type": "message_start",
                "message": {"usage": {"input_tokens": 25, "output_tokens": 1,
                    "cache_read_input_tokens": 9}}}),
        );
        // 2. Anthropic message_delta (bare usage, growing output).
        merge_stream_usage(
            &mut acc,
            &json!({"type": "message_delta", "usage": {"output_tokens": 42}}),
        );
        // 3. Converse metadata (camelCase, no `type`).
        merge_stream_usage(
            &mut acc,
            &json!({"usage": {"inputTokens": 25, "outputTokens": 42,
                "cacheWriteInputTokens": 3}}),
        );
        assert_eq!(acc.input_tokens, 25);
        assert_eq!(acc.output_tokens, 42);
        assert_eq!(acc.cache_read_tokens, 9);
        assert_eq!(acc.cache_write_tokens, 3);
    }

    #[test]
    fn stream_merge_decodes_invoke_model_base64_chunks() {
        let inner = json!({"type": "message_delta",
            "usage": {"output_tokens": 17, "input_tokens": 12}});
        let b64 =
            base64::engine::general_purpose::STANDARD.encode(serde_json::to_vec(&inner).unwrap());
        let mut acc = Usage::default();
        merge_stream_usage(&mut acc, &json!({"bytes": b64, "p": "pad"}));
        assert_eq!(acc.output_tokens, 17);
        assert_eq!(acc.input_tokens, 12);
        // Garbage base64 is ignored, never panics.
        merge_stream_usage(&mut acc, &json!({"bytes": "!!!not-base64!!!"}));
        assert_eq!(acc.output_tokens, 17);
    }
}
