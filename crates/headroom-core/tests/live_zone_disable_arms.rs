//! Env-var kill-switch for the PR-B4 dispatch arms — dedicated
//! integration test file.
//!
//! `disabled_arms` (`crates/headroom-core/src/transforms/live_zone.rs`)
//! reads `HEADROOM_LIVE_ZONE_DISABLE_ARMS` once per process and latches
//! the parsed set behind a process-global `OnceLock` (the determinism
//! invariant — a run's arm-disable set cannot change mid-flight).
//! Environment variables are process-global too, so setting
//! `HEADROOM_LIVE_ZONE_DISABLE_ARMS` must happen in a test process that
//! does nothing else — any other test in the same binary that dispatches
//! `SourceCode` or `PlainText` content first would freeze the `OnceLock`
//! against whatever it saw at that point (most likely: unset, i.e. no
//! arms disabled). That's why this file holds exactly ONE `#[test]` fn:
//! a dedicated file is a dedicated test binary, so no other test can
//! race the `OnceLock` initialization. Same isolation reasoning as
//! `live_zone_kompress_absent.rs`.
//!
//! Do NOT add more tests to this file.

use headroom_core::transforms::live_zone::DEFAULT_MODEL;
use headroom_core::transforms::{
    compress_anthropic_live_zone, AuthMode, BlockAction, LiveZoneOutcome,
};
use serde_json::{json, Value};

fn body_of(value: Value) -> Vec<u8> {
    serde_json::to_vec(&value).unwrap()
}

fn dispatch(body: &[u8]) -> LiveZoneOutcome {
    compress_anthropic_live_zone(body, 0, AuthMode::Payg, DEFAULT_MODEL)
        .expect("dispatcher returns Ok on valid bodies")
}

/// Build a body with one user message containing one `tool_result` whose
/// `content` is `text`. Same shape as `live_zone_dispatch.rs`'s helper of
/// the same name — duplicated here rather than shared, since integration
/// test files are independent crates (see module doc for why this file
/// must stand alone).
fn body_with_tool_result(text: &str) -> Vec<u8> {
    body_of(json!({
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_disable_arms_test",
                "content": text,
            }],
        }],
    }))
}

/// Build a syntactically valid Python module with `n` small functions —
/// long enough to clear the SourceCode byte threshold (2048) and, if the
/// CodeAwareCompressor ran, dense enough to shrink. Same generator style
/// as `live_zone_dispatch.rs::python_module_source` (duplicated, not
/// shared — see module doc).
fn python_module_source(n: usize) -> String {
    let mut code = String::new();
    code.push_str("\"\"\"Example data-processing module used by the disable-arms test.\"\"\"\n");
    code.push('\n');
    code.push_str("import json\n");
    code.push_str("import os\n");
    code.push_str("from typing import Any, Optional\n");
    code.push_str("\n\n");
    for i in 0..n {
        code.push_str(&format!("def process_record_{i}(record: dict) -> dict:\n"));
        code.push_str(&format!(
            "    \"\"\"Normalize record {i} and compute its derived fields.\"\"\"\n"
        ));
        code.push_str("    result = dict(record)\n");
        code.push_str(&format!("    result[\"index\"] = {i}\n"));
        code.push_str("    result[\"doubled\"] = record.get(\"value\", 0) * 2\n");
        code.push_str("    result[\"source\"] = \"batch\"\n");
        code.push_str("    if result[\"doubled\"] > 100:\n");
        code.push_str("        result[\"flag\"] = \"high\"\n");
        code.push_str("    else:\n");
        code.push_str("        result[\"flag\"] = \"low\"\n");
        code.push_str("    return result\n");
        code.push_str("\n\n");
    }
    code
}

/// Build repetitive plain prose at least `min_bytes` long. Same
/// repetition style as `live_zone_dispatch.rs`'s Kompress fixture
/// (duplicated, not shared — see module doc for why this file must
/// stand alone).
fn plain_prose(min_bytes: usize) -> String {
    let mut text = String::new();
    let mut i = 0usize;
    while text.len() < min_bytes {
        text.push_str(&format!(
            "City officials announced today that the downtown revitalization \
             project will proceed as planned despite budget concerns raised \
             during round {i} of public comment. "
        ));
        i += 1;
    }
    text
}

#[test]
fn disabled_arms_route_to_no_op_others_unaffected() {
    // `source_code, plain_text, bogus_type` — the internal spaces
    // exercise the `token.trim()` path, and `bogus_type` exercises the
    // unknown-token branch (logged and ignored — must not panic; this is
    // covered implicitly by the test reaching completion, since
    // `disabled_arms()` parses this value on the very first
    // `arm_disabled` call below).
    std::env::set_var(
        "HEADROOM_LIVE_ZONE_DISABLE_ARMS",
        "source_code, plain_text, bogus_type",
    );

    // (a) SourceCode arm disabled: a >2048-byte Python tool_result must
    // NOT reach the CodeAwareCompressor — NoCompressionApplied, not
    // Compressed.
    let code = python_module_source(10);
    assert!(
        code.len() > 2048,
        "fixture must clear the SourceCode byte threshold (2048); got {} bytes",
        code.len()
    );
    let code_out = dispatch(&body_with_tool_result(&code));
    let code_manifest = match &code_out {
        LiveZoneOutcome::NoChange { manifest } => manifest,
        LiveZoneOutcome::Modified { manifest, .. } => panic!(
            "disabled SourceCode arm must not rewrite bytes; got Modified. manifest: {manifest:?}"
        ),
    };
    let code_action = code_manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result block present in manifest")
        .action
        .clone();
    assert!(
        matches!(code_action, BlockAction::NoCompressionApplied { .. }),
        "disabled SourceCode arm must yield NoCompressionApplied, got {code_action:?}"
    );

    // (b) PlainText arm disabled: a >5120-byte plain-prose tool_result
    // must NOT reach the PlainText compressor (Kompress) — NoCompressionApplied,
    // not Compressed. The fixture MUST clear THRESHOLD_PLAIN_TEXT (5120
    // bytes in live_zone.rs) — below that, `compress_one_block`'s
    // byte-threshold gate short-circuits before dispatch even runs, and
    // the assertion below would pass regardless of the kill switch. Do
    // NOT shrink this fixture below 5120 bytes.
    let prose = plain_prose(5200);
    assert!(
        prose.len() > 5120,
        "fixture must clear the PlainText byte threshold (5120); got {} bytes",
        prose.len()
    );
    let prose_out = dispatch(&body_with_tool_result(&prose));
    let prose_manifest = match &prose_out {
        LiveZoneOutcome::NoChange { manifest } => manifest,
        LiveZoneOutcome::Modified { manifest, .. } => panic!(
            "disabled PlainText arm must not rewrite bytes; got Modified. manifest: {manifest:?}"
        ),
    };
    let prose_action = prose_manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result block present in manifest")
        .action
        .clone();
    assert!(
        matches!(prose_action, BlockAction::NoCompressionApplied { .. }),
        "disabled PlainText arm must yield NoCompressionApplied, got {prose_action:?}"
    );

    // (c) Other arms unaffected: a >512-byte JSON-array tool_result must
    // still compress via smart_crusher (the kill switch only guards the
    // SourceCode / PlainText arms).
    let array_of_dicts: Vec<Value> = (0..200)
        .map(|i| {
            json!({
                "id": i,
                "status": "ok",
                "value": format!("repeat-pattern-{}", i % 3),
            })
        })
        .collect();
    let payload = serde_json::to_string(&array_of_dicts).unwrap();
    assert!(
        payload.len() > 512,
        "fixture must clear the JsonArray byte threshold (512); got {} bytes",
        payload.len()
    );
    let json_out = dispatch(&body_with_tool_result(&payload));
    let json_manifest = match &json_out {
        LiveZoneOutcome::Modified { manifest, .. } => manifest,
        LiveZoneOutcome::NoChange { manifest } => panic!(
            "smart_crusher arm must be unaffected by the SourceCode/PlainText kill switch; \
             got NoChange. manifest: {manifest:?}"
        ),
    };
    let json_action = json_manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result block present in manifest")
        .action
        .clone();
    match json_action {
        BlockAction::Compressed { strategy, .. } => {
            assert_eq!(
                strategy, "smart_crusher",
                "expected SmartCrusher dispatch, unaffected by the disabled SourceCode/PlainText arms"
            );
        }
        other => panic!("expected BlockAction::Compressed via smart_crusher, got {other:?}"),
    }
}
