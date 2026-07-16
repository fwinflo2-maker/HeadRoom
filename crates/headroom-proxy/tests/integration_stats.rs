//! Integration tests for the native savings stats surface:
//! `/stats`, `/stats/timeseries`, `/stats/events`, `/dashboard`,
//! and the per-lane recording hooks (deferred SSE finalization,
//! non-streaming JSON usage capture, failed-request accounting,
//! Bedrock camelCase usage).

mod common;

use std::time::Duration;

use aws_credential_types::Credentials;
use common::{start_proxy_with, start_proxy_with_state};
use serde_json::{json, Value};
use url::Url;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

/// The capture tasks finalize asynchronously after the client sees
/// the response — poll /stats until the predicate holds.
async fn wait_for_stats<F>(base: &str, pred: F) -> Value
where
    F: Fn(&Value) -> bool,
{
    let client = reqwest::Client::new();
    for _ in 0..100 {
        let v: Value = client
            .get(format!("{base}/stats"))
            .send()
            .await
            .expect("GET /stats")
            .json()
            .await
            .expect("stats json");
        if pred(&v) {
            return v;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("stats predicate not satisfied within 5s");
}

#[tokio::test]
async fn stats_endpoints_serve_locally_and_validate_input() {
    let upstream = MockServer::start().await;
    let proxy = start_proxy_with(&upstream.uri(), |_| {}).await;
    let client = reqwest::Client::new();

    let stats: Value = client
        .get(format!("{}/stats", proxy.url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(stats["proxy"], "headroom-rust");
    assert_eq!(stats["requests"]["total"], 0);
    // headway's unified-stats reader contract.
    assert!(stats["summary"]["api_requests"].is_u64());
    assert!(stats["tokens"]["proxy_compression_saved"].is_u64());
    assert!(stats["requests"]["cached"].is_u64());

    let ts: Value = client
        .get(format!("{}/stats/timeseries?bucket=day", proxy.url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ts["bucket"], "day");
    let bad = client
        .get(format!("{}/stats/timeseries?bucket=fortnight", proxy.url()))
        .send()
        .await
        .unwrap();
    assert_eq!(bad.status(), 400);

    let events: Value = client
        .get(format!("{}/stats/events", proxy.url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(events["events"].as_array().unwrap().is_empty());

    let dash = client
        .get(format!("{}/dashboard", proxy.url()))
        .send()
        .await
        .unwrap();
    assert_eq!(dash.status(), 200);
    let ct = dash
        .headers()
        .get("content-type")
        .unwrap()
        .to_str()
        .unwrap();
    assert!(ct.starts_with("text/html"), "content-type: {ct}");
    let body = dash.text().await.unwrap();
    assert!(body.contains("Headroom Proxy — Savings"));
    // Self-contained page: no external script/style/font loads.
    assert!(!body.contains("https://cdn."), "no CDN dependencies");
}

#[tokio::test]
async fn stats_disabled_falls_through_to_upstream() {
    let upstream = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/stats"))
        .respond_with(ResponseTemplate::new(200).set_body_string("python-proxy-stats"))
        .mount(&upstream)
        .await;
    let proxy = start_proxy_with(&upstream.uri(), |c| c.stats = false).await;

    let body = reqwest::Client::new()
        .get(format!("{}/stats", proxy.url()))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert_eq!(
        body, "python-proxy-stats",
        "with --stats=false the path must tunnel upstream (pre-feature behaviour)"
    );
}

#[tokio::test]
async fn anthropic_non_streaming_response_records_usage() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "msg_1",
            "type": "message",
            "model": "claude-sonnet-4-5-20250929",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10
            }
        })))
        .mount(&upstream)
        .await;
    // compression=true engages the buffered arm (mode stays Off —
    // recording must work in pure-passthrough deployments too).
    let proxy = start_proxy_with(&upstream.uri(), |c| c.compression = true).await;

    let resp = reqwest::Client::new()
        .post(format!("{}/v1/messages", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let _ = resp.bytes().await.unwrap(); // drain so the capture tee closes

    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    assert_eq!(stats["tokens"]["input"], 100);
    assert_eq!(stats["tokens"]["output"], 25);
    assert_eq!(stats["tokens"]["cache_read"], 40);
    assert_eq!(stats["tokens"]["cache_write"], 10);
    assert_eq!(stats["requests"]["failed"], 0);
    assert_eq!(stats["session_by_provider"]["anthropic"]["requests"], 1);
    // 40 cache reads on sonnet pricing → non-zero cache savings.
    assert!(stats["session"]["cache_savings_usd"].as_f64().unwrap() > 0.0);
    assert!(stats["session"]["input_cost_usd"].as_f64().unwrap() > 0.0);
    // Model attribution landed.
    assert_eq!(
        stats["lifetime_by_model"]["claude-sonnet-4-5-20250929"]["requests"],
        1
    );
    // Timeseries picked it up.
    let ts: Value = reqwest::Client::new()
        .get(format!("{}/stats/timeseries?bucket=hour", proxy.url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ts["points"][0]["requests"], 1);
}

/// Recording must not depend on the compression master switch —
/// it is OFF by default, and spend/cache observability is the whole
/// point of the feature on a passthrough deployment. The model is
/// learned from the response there (the request body is never
/// buffered), and compression savings are legitimately 0.
#[tokio::test]
async fn records_with_compression_off_using_model_from_response() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "msg_1",
            "type": "message",
            "model": "claude-sonnet-4-5-20250929",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 200, "output_tokens": 10,
                      "cache_read_input_tokens": 80}
        })))
        .mount(&upstream)
        .await;
    // Config::for_test defaults compression to false — same as the
    // shipped binary's default.
    let proxy = start_proxy_with(&upstream.uri(), |c| {
        assert!(!c.compression, "guard: this test covers compression OFF");
    })
    .await;

    let resp = reqwest::Client::new()
        .post(format!("{}/v1/messages", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let _ = resp.bytes().await.unwrap();

    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    assert_eq!(stats["tokens"]["input"], 200);
    assert_eq!(stats["tokens"]["cache_read"], 80);
    assert_eq!(stats["tokens"]["saved"], 0, "no compression ran → 0 saved");
    // Model came from the response envelope, so spend is priced.
    assert_eq!(
        stats["lifetime_by_model"]["claude-sonnet-4-5-20250929"]["requests"], 1,
        "model must be learned from the response when the request isn't buffered"
    );
    assert!(stats["session"]["input_cost_usd"].as_f64().unwrap() > 0.0);
    assert!(stats["session"]["cache_savings_usd"].as_f64().unwrap() > 0.0);
}

#[tokio::test]
async fn anthropic_sse_stream_records_deferred_usage() {
    let upstream = MockServer::start().await;
    let sse_body = concat!(
        "event: message_start\n",
        "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_1\",\"usage\":",
        "{\"input_tokens\":50,\"output_tokens\":1,\"cache_read_input_tokens\":12}}}\n\n",
        "event: content_block_delta\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":",
        "{\"type\":\"text_delta\",\"text\":\"hey\"}}\n\n",
        "event: message_delta\n",
        "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},",
        "\"usage\":{\"output_tokens\":30}}\n\n",
        "event: message_stop\n",
        "data: {\"type\":\"message_stop\"}\n\n",
    );
    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_raw(sse_body.as_bytes().to_vec(), "text/event-stream"),
        )
        .mount(&upstream)
        .await;
    let proxy = start_proxy_with(&upstream.uri(), |c| c.compression = true).await;

    let resp = reqwest::Client::new()
        .post(format!("{}/v1/messages", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 32,
            "stream": true,
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let _ = resp.bytes().await.unwrap();

    // Deferred recording: counts come from the stream's usage
    // frames, observed only after the stream closed.
    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    assert_eq!(stats["tokens"]["input"], 50);
    assert_eq!(
        stats["tokens"]["output"], 30,
        "output from message_delta, not 0"
    );
    assert_eq!(stats["tokens"]["cache_read"], 12);
}

#[tokio::test]
async fn failed_upstream_counts_but_accrues_no_savings() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/messages"))
        .respond_with(ResponseTemplate::new(529).set_body_json(json!({
            "type": "error",
            "error": {"type": "overloaded_error", "message": "overloaded"}
        })))
        .mount(&upstream)
        .await;
    let proxy = start_proxy_with(&upstream.uri(), |c| c.compression = true).await;

    let resp = reqwest::Client::new()
        .post(format!("{}/v1/messages", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "model": "claude-sonnet-4-5-20250929",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 529);
    let _ = resp.bytes().await.unwrap();

    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    assert_eq!(stats["requests"]["failed"], 1);
    assert_eq!(stats["tokens"]["saved"], 0);
    assert_eq!(stats["session"]["savings_usd"], 0.0);
    assert_eq!(stats["session"]["input_cost_usd"], 0.0);
    // Visible in the feed, flagged.
    let events: Value = reqwest::Client::new()
        .get(format!("{}/stats/events?limit=5", proxy.url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(events["events"][0]["failed"], true);
    assert_eq!(events["events"][0]["tokens_saved"], 0);
}

#[tokio::test]
async fn openai_chat_response_normalises_cached_tokens() {
    let upstream = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 90,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 60}
            }
        })))
        .mount(&upstream)
        .await;
    let proxy = start_proxy_with(&upstream.uri(), |c| c.compression = true).await;

    let resp = reqwest::Client::new()
        .post(format!("{}/v1/chat/completions", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let _ = resp.bytes().await.unwrap();

    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    // prompt_tokens INCLUDES cached — the ledger stores the split.
    assert_eq!(stats["tokens"]["input"], 30);
    assert_eq!(stats["tokens"]["cache_read"], 60);
    assert_eq!(stats["tokens"]["output"], 12);
    assert_eq!(stats["session_by_provider"]["openai"]["requests"], 1);
}

fn test_credentials() -> Credentials {
    Credentials::new(
        "AKIAEXAMPLEAKIDFORTEST",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        None,
        None,
        "test",
    )
}

#[tokio::test]
async fn bedrock_converse_sync_records_camelcase_usage() {
    let upstream = MockServer::start().await;
    let model_id = "anthropic.claude-3-5-haiku-20241022-v1:0";
    Mock::given(method("POST"))
        .and(path(format!("/model/{model_id}/converse")))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "output": {"message": {"role": "assistant",
                "content": [{"text": "hello"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 9, "outputTokens": 4,
                      "cacheReadInputTokens": 2, "cacheWriteInputTokens": 1}
        })))
        .mount(&upstream)
        .await;

    let endpoint: Url = upstream.uri().parse().unwrap();
    let proxy = start_proxy_with_state(
        &upstream.uri(),
        |c| c.bedrock_endpoint = Some(endpoint),
        |s| s.with_bedrock_credentials(test_credentials()),
    )
    .await;

    let resp = reqwest::Client::new()
        .post(format!("{}/model/{model_id}/converse", proxy.url()))
        .header("content-type", "application/json")
        .json(&json!({
            "messages": [{"role": "user", "content": [{"text": "hello"}]}]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let _ = resp.bytes().await.unwrap();

    let stats = wait_for_stats(&proxy.url(), |v| v["requests"]["total"] == 1).await;
    assert_eq!(stats["session_by_provider"]["bedrock"]["requests"], 1);
    assert_eq!(stats["tokens"]["input"], 9);
    assert_eq!(stats["tokens"]["output"], 4);
    assert_eq!(stats["tokens"]["cache_read"], 2);
    assert_eq!(stats["tokens"]["cache_write"], 1);
    // Bedrock model ids price via the vendored table (cache reads
    // discounted vs list input price).
    assert!(stats["session"]["cache_savings_usd"].as_f64().unwrap() > 0.0);
    assert_eq!(stats["lifetime_by_model"][model_id]["requests"], 1);
}
