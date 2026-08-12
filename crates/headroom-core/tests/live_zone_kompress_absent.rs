//! Model-absent NoOp — dedicated integration test file.
//!
//! `kompress_cached` (`crates/headroom-core/src/transforms/live_zone.rs`)
//! memoizes its result behind a process-global `OnceLock`, and the HF
//! cache lookup it performs reads `HF_HUB_CACHE` / `HF_HOME` / `HOME` /
//! `USERPROFILE` at first call. Environment variables are process-global
//! too, so forcing those four vars to a cold, empty cache dir must happen
//! in a test process that does nothing else — any other test in the same
//! binary that dispatches PlainText content first would freeze the
//! `OnceLock` against the *ambient* environment instead. That's why this
//! file holds exactly ONE `#[test]` fn: a dedicated file is a dedicated
//! test binary, so no other test can race the `OnceLock` initialization.
//!
//! Do NOT add more tests to this file, and do NOT move this test into
//! `live_zone_dispatch.rs` (see `plain_text_routes_to_kompress_when_model_cached`
//! there, which relies on `kompress_cached` observing the *ambient*
//! cache).

use headroom_core::transforms::live_zone::DEFAULT_MODEL;
use headroom_core::transforms::{
    compress_anthropic_live_zone, AuthMode, BlockAction, LiveZoneOutcome,
};
use serde_json::json;

#[test]
fn plain_text_model_absent_is_deterministic_no_op() {
    // Force all four HF cache roots the loader consults
    // (kompress.rs:614-632) to a fresh, empty temp dir, so
    // `Kompress::from_cache` deterministically returns `Ok(None)` — the
    // cache-cold path — rather than picking up whatever happens to be in
    // the real user cache on the machine running this test.
    let cold_dir = tempfile::tempdir().expect("create fresh temp dir for cold HF cache");
    let cold_path = cold_dir.path().to_str().expect("temp dir path is UTF-8");
    for var in ["HF_HUB_CACHE", "HF_HOME", "HOME", "USERPROFILE"] {
        std::env::set_var(var, cold_path);
    }

    // > 5120 bytes of plain prose so the PlainText byte threshold is
    // cleared and the dispatcher actually attempts the Kompress arm
    // (rather than short-circuiting on `BelowByteThreshold`).
    let mut prose = String::new();
    while prose.len() <= 5120 {
        prose.push_str(
            "The archive committee reviewed every submission twice before \
             filing it, and the process took considerably longer than \
             anyone on the team had originally budgeted for. ",
        );
    }

    let body = serde_json::to_vec(&json!({
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 64,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_kompress_absent_test",
                "content": prose,
            }],
        }],
    }))
    .unwrap();

    let out = compress_anthropic_live_zone(&body, 0, AuthMode::Payg, DEFAULT_MODEL)
        .expect("dispatcher returns Ok on valid bodies; cache-cold Kompress must not error");

    let manifest = match &out {
        LiveZoneOutcome::NoChange { manifest } => manifest,
        LiveZoneOutcome::Modified { manifest, .. } => panic!(
            "cache-cold Kompress must not rewrite bytes; got Modified. manifest: {manifest:?}"
        ),
    };
    let action = manifest
        .block_outcomes
        .iter()
        .find(|b| b.block_type == "tool_result")
        .expect("tool_result block present in manifest")
        .action
        .clone();

    assert!(
        matches!(action, BlockAction::NoCompressionApplied { .. }),
        "cache-cold Kompress must degrade to a deterministic NoOp, not an error: {action:?}"
    );
}
