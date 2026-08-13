//! Property coverage for the PR-B4 dispatch arms (`SourceCode` →
//! `CodeAwareCompressor`, `PlainText` → Kompress, cache-only) plus the
//! round-trip test that closes the kill-switch alias bug class (task
//! R6 Part 0).
//!
//! Background: `content_type_from_name`
//! (`crates/headroom-core/src/transforms/live_zone.rs`) used to accept
//! only each `ContentType` variant's `as_str()` tag, so an
//! `HEADROOM_LIVE_ZONE_DISABLE_ARMS` token spelled the "natural" way a
//! human is more likely to write (e.g. `plain_text`, `search_results`)
//! silently failed to match. The token fell through to the
//! unknown-token branch, the arm was never disabled, and there was no
//! error — a misconfiguration that looked like a no-op. `PlainText`
//! (`"text"` / `"plain_text"`) was fixed first; this file's round-trip
//! test below pins the fix for the three remaining variants
//! (`SearchResults`, `BuildOutput`, `GitDiff`) and is written so that
//! adding an 8th `ContentType` variant later forces a compile-time
//! visit (see `natural_name_for`'s exhaustive match).
//!
//! Property-test coverage for the dispatch arms themselves follows
//! below: no-panic and determinism properties (proptest), plus a
//! byte-fidelity test for the SourceCode arm cloned from
//! `live_zone_dispatch.rs::byte_fidelity_outside_compressed_block`.
//! House style for the proptest blocks mirrors
//! `live_zone_token_validation.rs:201-254` — dispatch only through the
//! public `compress_anthropic_live_zone` entry point over generated
//! `tool_result` bodies, never the private `dispatch_compressor` — and
//! the no-panic parser fuzz tests in
//! `headroom-proxy/tests/sse_framing.rs:156-200` for case-count order
//! of magnitude and the comment style explaining the choice.

use headroom_core::transforms::live_zone::{content_type_from_name, DEFAULT_MODEL};
use headroom_core::transforms::{
    compress_anthropic_live_zone, AuthMode, BlockAction, ContentType, LiveZoneOutcome,
};
use proptest::prelude::*;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

fn body_of(value: Value) -> Vec<u8> {
    serde_json::to_vec(&value).unwrap()
}

fn dispatch(body: &[u8]) -> LiveZoneOutcome {
    compress_anthropic_live_zone(body, 0, AuthMode::Payg, DEFAULT_MODEL)
        .expect("dispatcher returns Ok on valid bodies")
}

/// Find the byte range of the FIRST occurrence of `needle` inside
/// `haystack`. Same helper as `live_zone_dispatch.rs::find_byte_range`
/// (duplicated, not shared — each integration test file is its own
/// crate).
fn find_byte_range(haystack: &[u8], needle: &[u8]) -> (usize, usize) {
    let pos = haystack
        .windows(needle.len().max(1))
        .position(|w| w == needle)
        .unwrap_or_else(|| {
            panic!(
                "needle of {} bytes not found in haystack of {} bytes",
                needle.len(),
                haystack.len()
            )
        });
    (pos, pos + needle.len())
}

fn sha256(bytes: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize().into()
}

/// Build a body with one user message containing one `tool_result`
/// whose `content` is `text`. Returns the full body and the byte
/// range of the JSON-encoded `content` slot (including surrounding
/// quotes). Same shape as `live_zone_dispatch.rs::body_with_tool_result`
/// (duplicated, not shared — integration test files are independent
/// crates).
fn body_with_tool_result(text: &str) -> (Vec<u8>, (usize, usize)) {
    let body = body_of(json!({
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "system": "you are a helpful assistant",
        "messages": [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_arm_properties_test",
                "content": text,
            }],
        }],
    }));
    let needle = serde_json::to_vec(&text).unwrap();
    let range = find_byte_range(&body, &needle);
    (body, range)
}

/// Build a syntactically valid Python module with `n` small functions
/// — long enough to clear the SourceCode byte threshold (2048) and
/// dense enough for the CodeAwareCompressor to shrink. Same generator
/// style as `live_zone_dispatch.rs::python_module_source` (duplicated,
/// not shared).
fn python_module_source(n: usize) -> String {
    let mut code = String::new();
    code.push_str(
        "\"\"\"Example data-processing module used by the arm-properties tests.\"\"\"\n",
    );
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

// ─── Part 0: kill-switch alias round trip (bug class extinction) ──────

/// Natural-name alias for each `ContentType` variant — the spelling a
/// human operator is more likely to write in
/// `HEADROOM_LIVE_ZONE_DISABLE_ARMS`. For variants whose `as_str()` tag
/// already reads naturally (`JsonArray` → `"json_array"`, `SourceCode`
/// → `"source_code"`, `Html` → `"html"`) the "alias" is the same
/// string; `content_type_from_name` doesn't need a second match arm
/// for those; there's nothing to fix (and this test still checks
/// them, so a future regression there would be caught too).
///
/// Deliberately an exhaustive match with no wildcard arm: adding an
/// 8th `ContentType` variant without touching this function is a
/// compile error, which forces whoever adds it to also decide its
/// natural-name alias (and, by extension, to re-examine
/// `content_type_from_name`).
fn natural_name_for(content_type: ContentType) -> &'static str {
    match content_type {
        ContentType::JsonArray => "json_array",
        ContentType::SourceCode => "source_code",
        ContentType::SearchResults => "search_results",
        ContentType::BuildOutput => "build_output",
        ContentType::GitDiff => "git_diff",
        ContentType::Html => "html",
        ContentType::PlainText => "plain_text",
    }
}

/// The bug class this test makes extinct: `content_type_from_name`
/// silently rejecting a valid natural-name spelling of a
/// `ContentType`, so `HEADROOM_LIVE_ZONE_DISABLE_ARMS` looks like it
/// disabled an arm but didn't. For EVERY variant, both the
/// `as_str()` tag and the natural-name alias must parse back to that
/// same variant.
#[test]
fn content_type_from_name_round_trips_all_variants() {
    // Explicit array of all 7 variants (not `ContentType::VARIANTS` or
    // similar — the enum has no such helper, and spelling them out is
    // the point: it's the thing a reviewer can visually check against
    // the enum definition in content_detector.rs).
    let variants: [ContentType; 7] = [
        ContentType::JsonArray,
        ContentType::SourceCode,
        ContentType::SearchResults,
        ContentType::BuildOutput,
        ContentType::GitDiff,
        ContentType::Html,
        ContentType::PlainText,
    ];

    for content_type in variants {
        let tag = content_type.as_str();
        assert_eq!(
            content_type_from_name(tag),
            Some(content_type),
            "as_str() tag {tag:?} must round-trip back to {content_type:?}"
        );

        let natural = natural_name_for(content_type);
        assert_eq!(
            content_type_from_name(natural),
            Some(content_type),
            "natural-name alias {natural:?} must parse to {content_type:?}"
        );
    }
}

// ─── Part 1: pathological-text generators (properties 1 & 2) ──────────

/// Pure-ASCII source fragment used by the "half-truncated code
/// snippet" arm of [`pathological_text`]. ASCII-only so every byte
/// index is also a valid `char` boundary — slicing at an arbitrary
/// length can never panic on a UTF-8 boundary violation.
const CODE_FRAGMENT: &str = "def handler(event, context):\n    \
payload = json.loads(event[\"body\"])\n    \
if payload.get(\"kind\") == \"ping\":\n        \
return {\"statusCode\": 200, \"body\": \"pong\"}\n    \
result = process(payload)\n    \
return {\"statusCode\": 200, \"body\": json.dumps(result)}\n";

/// Strategy generating "pathological" text for the dispatcher's
/// no-panic and determinism properties below. Mixes:
///
/// - plain arbitrary strings (the common case, still worth covering),
/// - control characters (0x00-0x1F) — the sort of byte a shell or log
///   scraper can hand a tool_result without ever going through a
///   terminal's escaping,
/// - unpaired UTF-16 surrogates, repaired via `String::from_utf16_lossy`
///   (a Rust `String` can't hold an actual lone surrogate — this is
///   the closest in-process torture test: what a naive UTF-16 → UTF-8
///   bridge upstream would hand us after "fixing" a bad pair),
/// - half-truncated code snippets (a coding agent's tool output cut
///   off mid-token is a realistic production shape, not just a fuzz
///   artifact),
/// - very long single lines with no newlines (minified JS, a base64
///   blob, ...).
fn pathological_text() -> impl Strategy<Value = String> {
    prop_oneof![
        3 => any::<String>(),
        2 => proptest::collection::vec(0u8..0x20u8, 0..256)
            .prop_map(|bytes| bytes.into_iter().map(|b| b as char).collect()),
        2 => proptest::collection::vec(any::<u16>(), 0..128)
            .prop_map(|units| String::from_utf16_lossy(&units)),
        1 => (0..=CODE_FRAGMENT.len()).prop_map(|n| CODE_FRAGMENT[..n].to_string()),
        1 => (1_000usize..8_000).prop_map(|n| "x".repeat(n)),
    ]
}

// The dispatcher must never panic on arbitrary text, however
// pathological, and must be deterministic. Case counts kept in the
// same order of magnitude as `sse_framing.rs`'s parser fuzz tests
// (1024-4096) so `cargo test --workspace` stays practical in CI; the
// no-panic property gets the larger count since it's the property
// most likely to catch a real crash, the determinism property the
// smaller since each case dispatches twice.
//
// Kompress may be cache-cold on this machine (the HF cache is
// per-machine, not part of the repo) — both properties below must
// hold regardless: `kompress_or_noop` degrades to a deterministic
// NoOp when the model isn't cache-resident, which is a valid outcome
// for both "never panics" and "deterministic", not a test dependency
// on the model being loaded.

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 2_048,
        // Give the shrinker room to minimize any panic it finds down
        // to a small repro instead of giving up early.
        max_shrink_iters: 1024,
        ..ProptestConfig::default()
    })]

    /// Property 1: for arbitrary (including pathological) `String`s
    /// embedded as a `tool_result` body, dispatching through the
    /// public `compress_anthropic_live_zone` entry point must never
    /// panic.
    #[test]
    fn dispatch_no_panic_on_arbitrary_text(text in pathological_text()) {
        let (body, _) = body_with_tool_result(&text);
        // `dispatch` itself panics (via `.expect`) only on a
        // dispatcher `Err`, which a well-formed JSON body constructed
        // above can't produce. Reaching the end of this closure
        // without unwinding IS the property.
        let _ = dispatch(&body);
    }
}

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 1_024,
        ..ProptestConfig::default()
    })]

    /// Property 2: determinism. The dispatcher's only process-global
    /// state is the `HEADROOM_LIVE_ZONE_DISABLE_ARMS` kill-switch set
    /// (unset in this file, and latched once regardless), so the same
    /// input bytes must always produce the same output bytes AND the
    /// same manifest. `CompressionManifest` / `BlockAction` are
    /// `Debug`-only (no `PartialEq` — they're observability types, not
    /// meant for equality comparisons in production code), so this
    /// compares their `Debug` renderings as a structural-equality
    /// proxy, the standard workaround for that situation.
    #[test]
    fn dispatch_deterministic_same_bytes(text in pathological_text()) {
        let (body, _) = body_with_tool_result(&text);

        let out1 = dispatch(&body);
        let out2 = dispatch(&body);

        let (bytes1, manifest1) = match &out1 {
            LiveZoneOutcome::NoChange { manifest } => (body.clone(), format!("{manifest:?}")),
            LiveZoneOutcome::Modified { new_body, manifest } => {
                (new_body.get().as_bytes().to_vec(), format!("{manifest:?}"))
            }
        };
        let (bytes2, manifest2) = match &out2 {
            LiveZoneOutcome::NoChange { manifest } => (body.clone(), format!("{manifest:?}")),
            LiveZoneOutcome::Modified { new_body, manifest } => {
                (new_body.get().as_bytes().to_vec(), format!("{manifest:?}"))
            }
        };

        prop_assert_eq!(
            &bytes1, &bytes2,
            "same input bytes must yield the same output bytes (bytes in -> bytes out)"
        );
        prop_assert_eq!(
            manifest1, manifest2,
            "same input bytes must yield the same manifest"
        );
    }
}

// ─── Part 1, property 3: byte fidelity around the SourceCode arm ──────

#[test]
fn byte_fidelity_outside_compressed_source_block() {
    // Same central invariant as `live_zone_dispatch.rs`'s
    // `byte_fidelity_outside_compressed_block` (the B3 SmartCrusher
    // pin), cloned onto the PR-B4 SourceCode/CodeAwareCompressor arm:
    // bytes OUTSIDE the rewritten block must hash byte-identical to
    // the input, regardless of which compressor did the rewriting.
    let code = python_module_source(10);
    assert!(
        code.len() > 2048,
        "fixture must clear the SourceCode byte threshold (2048); got {} bytes",
        code.len()
    );

    let (body_in, content_range) = body_with_tool_result(&code);
    let (block_start, block_end) = content_range;

    let out = dispatch(&body_in);
    let (new_body, strategy) = match &out {
        LiveZoneOutcome::Modified { new_body, manifest } => {
            let action = manifest
                .block_outcomes
                .iter()
                .find(|b| b.block_type == "tool_result")
                .expect("tool_result block present in manifest")
                .action
                .clone();
            let strategy = match action {
                BlockAction::Compressed { strategy, .. } => strategy,
                other => panic!(
                    "expected Compressed action for a 10-function Python module, got {other:?}"
                ),
            };
            (new_body.get().as_bytes().to_vec(), strategy)
        }
        LiveZoneOutcome::NoChange { manifest } => panic!(
            "expected CodeAwareCompressor to shrink a 10-function Python module; \
             got NoChange. manifest: {manifest:?}"
        ),
    };
    assert_eq!(
        strategy, "code_compressor",
        "expected code_compressor dispatch for SourceCode content"
    );

    // Prefix bytes (before the content slot) must be byte-identical.
    let in_prefix = &body_in[..block_start];
    let out_prefix = &new_body[..block_start];
    assert_eq!(
        sha256(in_prefix),
        sha256(out_prefix),
        "prefix bytes outside the compressed block must be byte-equal"
    );

    // Suffix length will differ by the compression delta, so locate
    // the suffix in the output by length: it's the trailing
    // (in.len() - block_end) bytes.
    let in_suffix_len = body_in.len() - block_end;
    let in_suffix = &body_in[block_end..];
    let out_suffix = &new_body[new_body.len() - in_suffix_len..];
    assert_eq!(
        sha256(in_suffix),
        sha256(out_suffix),
        "suffix bytes outside the compressed block must be byte-equal"
    );

    // Output must still be valid JSON with the untouched top-level
    // fields intact.
    let parsed: Value = serde_json::from_slice(&new_body).expect("output is valid JSON");
    assert_eq!(parsed["model"], "claude-sonnet-4-6");
    assert_eq!(parsed["system"], "you are a helpful assistant");
}
