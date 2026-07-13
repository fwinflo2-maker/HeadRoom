//! Freshness exemption — integration tests for issue #3.
//!
//! `compress_anthropic_live_zone_with_ccr_and_freshness` must never
//! compress the most-recent `fresh_message_count` messages (counted
//! from the tail) regardless of size — that content is in-flight,
//! about to be read by the model for the very first time this exact
//! request. Older, non-frozen messages remain fully eligible.

use headroom_core::transforms::live_zone::{
    compress_anthropic_live_zone_with_ccr, compress_anthropic_live_zone_with_ccr_and_freshness,
    DEFAULT_MODEL,
};
use headroom_core::transforms::{AuthMode, BlockAction, LiveZoneOutcome};
use serde_json::{json, Value};

fn body_of(value: Value) -> Vec<u8> {
    serde_json::to_vec(&value).unwrap()
}

fn large_json_array_payload() -> String {
    let items: Vec<Value> = (0..400)
        .map(|i| {
            json!({
                "id": i,
                "kind": "row",
                "status": "ok",
                "value": format!("repeat-{}", i % 5),
            })
        })
        .collect();
    let payload = serde_json::to_string(&items).unwrap();
    assert!(
        payload.len() >= 10_000,
        "fixture must clear the byte threshold"
    );
    payload
}

fn manifest_of(out: &LiveZoneOutcome) -> &headroom_core::transforms::CompressionManifest {
    match out {
        LiveZoneOutcome::NoChange { manifest } => manifest,
        LiveZoneOutcome::Modified { manifest, .. } => manifest,
    }
}

#[test]
fn large_fresh_tool_result_passes_through_verbatim() {
    // Single user message, large tool_result — but it's the ONLY
    // (hence tail, hence in-flight/fresh) message. With
    // fresh_message_count=1 the dispatcher must find no compressible
    // target at all and emit NoChange, no matter how large the
    // payload is.
    let payload = large_json_array_payload();
    let body = body_of(json!({
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_fresh",
                "content": payload,
            }],
        }],
    }));

    let out = compress_anthropic_live_zone_with_ccr_and_freshness(
        &body,
        0,
        1,
        AuthMode::Payg,
        DEFAULT_MODEL,
        None,
    )
    .expect("dispatcher returns Ok on valid bodies");

    assert!(
        matches!(out, LiveZoneOutcome::NoChange { .. }),
        "fresh (tail) message must never be compressed, regardless of size"
    );
    assert_eq!(
        manifest_of(&out).latest_user_message_index,
        None,
        "the only user message is fresh — no compression target should be found"
    );
}

#[test]
fn same_body_without_freshness_gate_does_compress() {
    // Sanity/contrast: the exact same body, dispatched through the
    // freshness-unaware entry point (fresh_message_count=0 baked in),
    // DOES get compressed. Proves the freshness param — not something
    // else — is what caused the exemption above.
    let payload = large_json_array_payload();
    let body = body_of(json!({
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_fresh",
                "content": payload,
            }],
        }],
    }));

    let out = compress_anthropic_live_zone_with_ccr(&body, 0, AuthMode::Payg, DEFAULT_MODEL, None)
        .expect("dispatcher returns Ok on valid bodies");

    let manifest = manifest_of(&out);
    assert_eq!(manifest.latest_user_message_index, Some(0));
    let action = manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result present")
        .action
        .clone();
    match action {
        BlockAction::Compressed { .. } | BlockAction::RejectedNotSmaller { .. } => {}
        other => panic!("expected the dispatcher to attempt compression, got {other:?}"),
    }
}

#[test]
fn large_aged_tool_result_still_compresses_while_fresh_tail_is_untouched() {
    // Three messages: [aged user tool_result (large), assistant text,
    // fresh user tool_result (tail, large)]. With fresh_message_count=1
    // the dispatcher must target the AGED message (index 0) — the
    // fresh tail (index 2) is excluded from the search window
    // entirely and stays byte-identical.
    let aged_payload = large_json_array_payload();
    let fresh_payload = large_json_array_payload();
    let body = body_of(json!({
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_aged",
                    "content": aged_payload,
                }],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok, one more lookup"}],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_fresh",
                    "content": fresh_payload,
                }],
            },
        ],
    }));

    let out = compress_anthropic_live_zone_with_ccr_and_freshness(
        &body,
        0,
        1,
        AuthMode::Payg,
        DEFAULT_MODEL,
        None,
    )
    .expect("dispatcher returns Ok on valid bodies");

    let manifest = manifest_of(&out);
    assert_eq!(
        manifest.latest_user_message_index,
        Some(0),
        "dispatcher must target the aged message, skipping the fresh tail entirely"
    );
    let action = manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result present")
        .action
        .clone();
    match action {
        BlockAction::Compressed { .. } | BlockAction::RejectedNotSmaller { .. } => {}
        other => panic!("expected the aged message to attempt compression, got {other:?}"),
    }

    // The fresh tail's payload must survive byte-identical in the
    // output (if the body was rewritten at all).
    if let LiveZoneOutcome::Modified { new_body, .. } = &out {
        let new_body_str = new_body.get();
        assert!(
            new_body_str.contains(&fresh_payload.replace('"', "\\\""))
                || new_body_str.contains(&fresh_payload),
            "fresh tail payload must be untouched in the rewritten body"
        );
    }
}
