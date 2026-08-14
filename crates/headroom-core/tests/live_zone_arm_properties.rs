//! Property coverage for the PR-B4 dispatch arms (`SourceCode` →
//! `CodeAwareCompressor`, `PlainText` → Kompress, cache-only) plus the
//! round-trip test that closes the kill-switch alias bug class.
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
//! (`SearchResults`, `BuildOutput`, `GitDiff`).
//!
//! **Review note:** an earlier version of this comment claimed
//! that the round-trip test's exhaustive `natural_name_for` match alone
//! "makes adding an 8th `ContentType` variant force a compile-time
//! visit" in a way that keeps the test's coverage complete. A review
//! proved that overclaimed: the exhaustive match only forces picking
//! *a* string for the new variant, not adding it to the array the
//! round-trip test iterates. See the honesty note on `natural_name_for`
//! below for the corrected, precise claim and why a fully
//! compiler-forced guarantee isn't achievable here in stable Rust.
//!
//! Property-test coverage for the dispatch arms themselves follows
//! below: no-panic and determinism properties (proptest), plus a
//! byte-fidelity test for the SourceCode arm cloned from
//! `live_zone_dispatch.rs::byte_fidelity_outside_compressed_block`, plus
//! an instrumentation test that measures — rather than
//! asserts — what fraction of generated cases actually reach a dispatch
//! arm, broken down by content type. House style for the proptest
//! blocks mirrors `live_zone_token_validation.rs:201-254` — dispatch
//! only through the public `compress_anthropic_live_zone` entry point
//! over generated `tool_result` bodies, never the private
//! `dispatch_compressor` — and the no-panic parser fuzz tests in
//! `headroom-proxy/tests/sse_framing.rs:156-200` for case-count order
//! of magnitude and the comment style explaining the choice.

use headroom_core::transforms::live_zone::{content_type_from_name, DEFAULT_MODEL};
use headroom_core::transforms::{
    compress_anthropic_live_zone, detect_content_type, AuthMode, BlockAction, ContentType,
    LiveZoneOutcome,
};
use proptest::prelude::*;
use proptest::strategy::ValueTree;
use proptest::test_runner::{RngAlgorithm, TestRng, TestRunner};
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
    code.push_str("\"\"\"Example data-processing module used by the arm-properties tests.\"\"\"\n");
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

/// Single source of truth: every `ContentType` variant paired with the
/// natural-name alias an operator is more likely to write in
/// `HEADROOM_LIVE_ZONE_DISABLE_ARMS`. Both round-trip assertions in
/// `content_type_from_name_round_trips_all_variants` iterate THIS array
/// — not a separately-maintained list — so there is exactly one place
/// (plus `natural_name_for` below, kept only as an independent
/// cross-check) that has to be edited when a variant's alias changes.
///
/// This replaces an earlier scheme, where the round-trip
/// test carried its own hand-written `[ContentType; 7]` array
/// completely independent of `natural_name_for`'s match. See the
/// honesty note on `natural_name_for` for why this still isn't a fully
/// compiler-forced guarantee against a variant going missing here.
const VARIANT_TABLE: [(ContentType, &str); 7] = [
    (ContentType::JsonArray, "json_array"),
    (ContentType::SourceCode, "source_code"),
    (ContentType::SearchResults, "search_results"),
    (ContentType::BuildOutput, "build_output"),
    (ContentType::GitDiff, "git_diff"),
    (ContentType::Html, "html"),
    (ContentType::PlainText, "plain_text"),
];

/// Manually mirrors `ContentType`'s variant count (content_detector.rs).
/// The compile-time assertion below catches `VARIANT_TABLE`'s own
/// `[(..); N]` length annotation drifting from THIS number (e.g. a row
/// is added but the array-size annotation isn't bumped to match, which
/// would otherwise be a plain array-literal-length compile error anyway
/// — so this mostly documents intent). It cannot verify that `7` is
/// still `ContentType`'s TRUE variant count: Rust has no stable
/// reflection API for "how many variants does this enum have" (that is
/// exactly the gap crates like `strum`'s `EnumCount` derive macro exist
/// to close), and pulling in such a crate is out of scope for a fix
/// confined to this single test file. This is the strongest
/// compile-time check available under that constraint, not a full
/// guarantee — see `natural_name_for`'s honesty note for the complete
/// picture of what is and isn't actually enforced.
const CONTENT_TYPE_VARIANT_COUNT: usize = 7;
const _: () = assert!(VARIANT_TABLE.len() == CONTENT_TYPE_VARIANT_COUNT);

/// Natural-name alias for each `ContentType` variant, expressed as an
/// exhaustive match — kept as an INDEPENDENT source from `VARIANT_TABLE`
/// (rather than generating one from the other) specifically so
/// `variant_table_agrees_with_natural_name_for` below is a real
/// cross-check and not a tautology.
///
/// Deliberately an exhaustive match with no wildcard arm: adding an 8th
/// `ContentType` variant without touching this function is a compile
/// error.
///
/// **Honesty note:** that compile
/// error is the ONLY part of this file's variant-coverage mechanism the
/// Rust compiler actually enforces. It forces a human to choose *some*
/// string for the new variant right here — it does NOT force them to
/// also add a row to `VARIANT_TABLE` above, or to bump
/// `CONTENT_TYPE_VARIANT_COUNT`. A reviewer proved this gap
/// empirically: in a scratch crate, adding a hypothetical 8th
/// `ContentType` variant and touching ONLY this function (picking an
/// arbitrary placeholder string to satisfy the compiler) left both the
/// production `content_type_from_name` (which necessarily ends in a
/// `_ => None` wildcard — string matches over an enum can't be
/// exhaustive) and the test's variant array untouched, and it compiled
/// clean with the round-trip test staying green while the original
/// alias bug reproduced for the new variant. Rust has no stable
/// "enumerate every variant of this enum" reflection primitive without
/// an external derive-macro crate (e.g. `strum`), and adding one is out
/// of scope for a fix confined to this test file. So: this exhaustive
/// match plus `VARIANT_TABLE` plus the two cross-check tests below is
/// the STRONGEST APPROXIMATION available in stable Rust without a new
/// dependency, not a fully compiler-forced guarantee. An earlier
/// version of this comment implied otherwise; corrected here per the
/// review.
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
/// disabled an arm but didn't. For EVERY variant in `VARIANT_TABLE`,
/// both the `as_str()` tag and the natural-name alias must parse back
/// to that same variant.
#[test]
fn content_type_from_name_round_trips_all_variants() {
    for &(content_type, natural) in VARIANT_TABLE.iter() {
        let tag = content_type.as_str();
        assert_eq!(
            content_type_from_name(tag),
            Some(content_type),
            "as_str() tag {tag:?} must round-trip back to {content_type:?}"
        );

        assert_eq!(
            content_type_from_name(natural),
            Some(content_type),
            "natural-name alias {natural:?} must parse to {content_type:?}"
        );
    }
}

/// Cross-check the two independently hand-maintained sources of
/// natural-name truth — `VARIANT_TABLE`'s literal strings and
/// `natural_name_for`'s exhaustive match — against each other for every
/// `VARIANT_TABLE` row. This catches the two drifting apart (e.g. a
/// typo fixed in one but not the other). It does NOT catch a variant
/// missing from `VARIANT_TABLE` altogether — see the honesty note on
/// `natural_name_for` for why that half of the guarantee isn't
/// achievable at compile time here.
#[test]
fn variant_table_agrees_with_natural_name_for() {
    for &(content_type, natural) in VARIANT_TABLE.iter() {
        assert_eq!(
            natural_name_for(content_type),
            natural,
            "VARIANT_TABLE says {content_type:?} -> {natural:?}, natural_name_for disagrees"
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

/// Function-name pool for the "code-like" generator bucket's varied
/// identifiers — deliberately NOT the
/// single `process_record_{i}` pattern `python_module_source` uses for
/// its fixed byte-fidelity fixture.
const CODE_IDENTIFIERS: &[&str] = &[
    "process_record",
    "normalize_entry",
    "compute_score",
    "parse_payload",
    "build_summary",
    "validate_input",
    "apply_filter",
    "merge_results",
    "extract_fields",
    "transform_row",
    "classify_item",
    "enrich_context",
    "dedupe_values",
    "flatten_tree",
    "sanitize_text",
    "batch_update",
    "load_config",
    "fetch_metadata",
    "index_documents",
    "score_candidate",
    "resolve_alias",
    "collect_stats",
    "prune_stale",
    "rank_matches",
    "join_segments",
    "split_batch",
    "verify_checksum",
    "encode_payload",
    "decode_payload",
    "queue_task",
];

/// Render one function: a docstring, then `body_lines` body statements
/// (floored at 2: `result = ...` plus `return result`), then a blank
/// line. `body_lines` deliberately spans both sides of
/// `CodeCompressorConfig::default().max_body_lines` (5) — a whole
/// function node of `<= max_body_lines + 2 == 7` lines passes through
/// `compress_function_ast` untouched, longer ones get their body
/// collapsed — so callers can generate a mix of both shapes in one
/// module instead of the "always long enough to collapse" shape
/// `python_module_source` uses.
fn render_function(name: &str, index: usize, body_lines: usize) -> String {
    let body_lines = body_lines.max(2);
    let filler = body_lines - 2;
    let mut out = String::new();
    out.push_str(&format!("def {name}_{index}(record: dict) -> dict:\n"));
    out.push_str(&format!(
        "    \"\"\"Derive fields for {name} #{index}.\"\"\"\n"
    ));
    out.push_str("    result = dict(record)\n");
    for i in 0..filler {
        out.push_str(&format!("    result[\"{name}_{i}\"] = {i}\n"));
    }
    out.push_str("    return result\n");
    out.push('\n');
    out
}

/// Syntactically-plausible Python, 2048-6000 bytes, with VARIED
/// structure: differing function counts and body lengths straddling
/// the CodeAwareCompressor's 5-line collapse floor, varied identifiers
/// drawn from `CODE_IDENTIFIERS`. Added after a review measured that
/// before this bucket existed,
/// 0% of `pathological_text()`'s generated cases ever reached the
/// SourceCode dispatch arm (the fixed `python_module_source(10)`
/// fixture in `byte_fidelity_outside_compressed_source_block` was
/// SourceCode's only exercise anywhere in this file).
///
/// Always classifies `SourceCode` — but NOT because of the header
/// alone: `try_detect_code`'s confidence is
/// `0.4 + (matching_lines / non_empty_lines) * 0.4 + matching_lines * 0.02`,
/// so a fixed count of header matches DILUTES below the 0.5 floor once
/// enough non-matching lines follow (~4 matches over 100 non-empty
/// lines ≈ 0.496). What actually holds classification is that every
/// `render_function` body emits `def ...:` / docstring lines that also
/// match `CODE_PATTERNS`, keeping `matching_lines` roughly proportional
/// to length. Consequence for future edits: rewriting `render_function`
/// with shapes that DON'T match the detector's patterns (e.g.
/// assignment-bound lambdas) would silently reclassify large samples as
/// PlainText — `dispatch_reach_fractions_meet_floor` fails loudly when
/// that happens; believe it, and look here first.
///
/// Byte-length mechanism: pick a `target_bytes` first, then append
/// functions from a pool of varied specs until the buffer crosses it
/// (each function bounded to at most ~510 bytes by `body_lines`'s 1..=10
/// range, so the overshoot past `target_bytes`'s 5000 ceiling can't
/// reach the outer 6000 bound). If the pool shrinks smaller than
/// `target_bytes` needs (proptest's shrinker actively tries this), a
/// deterministic filler function pads the buffer past the 2048 floor
/// regardless — this keeps the "always >= 2048 bytes, always
/// SourceCode" invariant true even for a minimized failing case, so a
/// shrunk counterexample stays inside the region that made it
/// interesting in the first place.
fn code_like_source() -> impl Strategy<Value = String> {
    (
        2_048usize..=5_000,
        proptest::collection::vec(
            (proptest::sample::select(CODE_IDENTIFIERS), 1usize..=10),
            1..=200,
        ),
    )
        .prop_map(|(target_bytes, specs)| {
            let mut code = String::from(
                "\"\"\"Generated module for the code-like dispatch-reach bucket.\"\"\"\n\n\
                 import json\n\
                 import os\n\
                 from typing import Any, Optional\n\n\n",
            );
            for (index, (name, body_lines)) in specs.into_iter().enumerate() {
                if code.len() >= target_bytes {
                    break;
                }
                code.push_str(&render_function(name, index, body_lines));
            }
            // Guaranteed floor (see doc comment above): pad with a
            // deterministic filler function so this bucket always
            // clears THRESHOLD_SOURCE_CODE (2048), even for a pool too
            // small (or too shrunk) to reach `target_bytes` on its own.
            let mut filler_index = 10_000usize;
            while code.len() < 2_048 {
                code.push_str(&render_function("filler", filler_index, 8));
                filler_index += 1;
            }
            code
        })
}

/// Vocabulary for the "prose-like" generator bucket: ordinary lowercase
/// English words chosen to have NO overlap with any content_detector.rs
/// keyword or symbol (`def`, `class`, `import`, `ERROR`, `diff`, `<`,
/// `[`, `:`, `@`, ...), so generated text is guaranteed to fall through
/// every specialized detector (JSON / diff / HTML / search / log /
/// code) and land on `PlainText`'s default branch.
const PROSE_WORDS: &[&str] = &[
    "the",
    "team",
    "reviewed",
    "quarterly",
    "report",
    "and",
    "found",
    "several",
    "interesting",
    "trends",
    "worth",
    "discussing",
    "further",
    "during",
    "next",
    "weeks",
    "meeting",
    "customers",
    "have",
    "been",
    "asking",
    "about",
    "new",
    "features",
    "that",
    "would",
    "improve",
    "their",
    "daily",
    "workflow",
    "without",
    "adding",
    "unnecessary",
    "complexity",
    "to",
    "existing",
    "processes",
    "engineers",
    "spent",
    "most",
    "of",
    "afternoon",
    "debugging",
    "a",
    "tricky",
    "issue",
    "related",
    "caching",
    "behavior",
    "under",
    "heavy",
    "load",
    "documentation",
    "was",
    "updated",
    "reflect",
    "recent",
    "changes",
    "in",
    "policy",
    "procedure",
    "across",
    "departments",
    "stakeholders",
    "expressed",
    "cautious",
    "optimism",
    "regarding",
    "timeline",
    "for",
    "launch",
    "despite",
    "lingering",
    "concerns",
    "budget",
    "allocation",
    "remains",
    "topic",
    "ongoing",
    "discussion",
    "among",
    "leadership",
    "training",
    "materials",
    "were",
    "distributed",
    "all",
    "staff",
    "ahead",
    "upcoming",
    "transition",
    "period",
    "feedback",
    "collected",
    "from",
    "survey",
    "suggests",
    "broad",
    "satisfaction",
    "with",
    "current",
    "support",
    "channels",
    "seasonal",
    "demand",
    "typically",
    "increases",
    "toward",
    "end",
    "fiscal",
    "year",
    "requiring",
    "additional",
    "planning",
    "resources",
    "roadmap",
    "priorities",
    "shifted",
    "slightly",
    "after",
    "user",
    "research",
    "revealed",
    "unexpected",
    "usage",
    "patterns",
    "warrant",
    "deeper",
    "study",
    "onboarding",
    "experience",
    "still",
    "feels",
    "clunky",
    "several",
    "early",
    "testers",
    "noted",
    "confusion",
    "around",
    "default",
    "settings",
    "release",
    "notes",
    "circulated",
    "internally",
    "before",
    "public",
    "announcement",
    "went",
    "out",
    "later",
    "same",
    "week",
];

/// Prose-like text, 5200-11800 bytes, built from `PROSE_WORDS` with
/// varied sentence/paragraph shape — deliberately NOT a single repeated
/// character, unlike the pre-existing `pathological_text()` bucket that
/// repeats `'x'` 1000-8000 times. Added by the same review as
/// `code_like_source`: the problem with the old repeated-`'x'`
/// bucket was that it only reaches the PlainText arm by luck of length
/// crossing 5120 bytes, not because it looks like prose — this bucket
/// looks like prose AND always clears the threshold.
///
/// Same target-then-fill-then-pad mechanism as `code_like_source`
/// (see its doc comment): the length floor (5200 bytes, comfortably
/// above `THRESHOLD_PLAIN_TEXT`'s 5120) holds even for a shrunk word
/// pool, so a minimized counterexample from this bucket stays inside
/// the region that made it interesting.
fn prose_like_text() -> impl Strategy<Value = String> {
    (
        5_200usize..=11_800,
        proptest::collection::vec(proptest::sample::select(PROSE_WORDS), 1..=2_200),
    )
        .prop_map(|(target_bytes, words)| {
            let mut text = String::with_capacity(target_bytes + 64);
            let mut words_in_sentence = 0u32;
            for word in words {
                if text.len() >= target_bytes {
                    break;
                }
                text.push_str(word);
                words_in_sentence += 1;
                // Vary sentence/paragraph shape instead of one giant
                // run-on line: end a "sentence" every ~9 words.
                if words_in_sentence >= 9 {
                    text.push_str(".\n");
                    words_in_sentence = 0;
                } else {
                    text.push(' ');
                }
            }
            while text.len() < 5_200 {
                text.push_str("padding ");
            }
            text
        })
}

/// Strategy generating "pathological" text for the dispatcher's
/// no-panic and determinism properties below. Mixes:
///
/// - plain arbitrary strings (the common case, still worth covering;
///   proptest 1.11.0's `any::<String>()` caps at 32 chars, so this
///   bucket never clears any dispatch threshold on its own),
/// - control characters (0x00-0x1F, capped ~256 bytes) — the sort of
///   byte a shell or log scraper can hand a tool_result without ever
///   going through a terminal's escaping,
/// - unpaired UTF-16 surrogates (capped ~384 bytes), repaired via
///   `String::from_utf16_lossy` (a Rust `String` can't hold an actual
///   lone surrogate — this is the closest in-process torture test: what
///   a naive UTF-16 → UTF-8 bridge upstream would hand us after
///   "fixing" a bad pair),
/// - half-truncated code snippets (capped ~272 bytes; a coding agent's
///   tool output cut off mid-token is a realistic production shape, not
///   just a fuzz artifact),
/// - very long single lines with no newlines, 1000-8000 chars (minified
///   JS, a base64 blob, ...) — before the two rich buckets below were
///   added, the only bucket that
///   could ever clear a threshold (PlainText's 5120, when the sampled
///   length lands >= 5120, ~41% of this bucket's own range),
/// - `code_like_source()` — syntactically
///   plausible, varied-structure Python that always clears the
///   SourceCode threshold (2048) and always detects as SourceCode,
/// - `prose_like_text()` — varied
///   natural-language-shaped text that always clears the PlainText
///   threshold (5120) and always detects as PlainText.
///
/// Weights are set so the two rich buckets carry
/// enough weight to matter (see `dispatch_reach_fractions_meet_floor`
/// for the measured, instrumented result) while keeping every small
/// pathological bucket from before — the no-panic envelope over
/// encoding/detection/threshold-comparison logic on genuinely
/// pathological BYTES (not just large-but-tame text) is still valuable
/// in its own right.
fn pathological_text() -> impl Strategy<Value = String> {
    prop_oneof![
        2 => any::<String>(),
        1 => proptest::collection::vec(0u8..0x20u8, 0..256)
            .prop_map(|bytes| bytes.into_iter().map(|b| b as char).collect()),
        1 => proptest::collection::vec(any::<u16>(), 0..128)
            .prop_map(|units| String::from_utf16_lossy(&units)),
        1 => (0..=CODE_FRAGMENT.len()).prop_map(|n| CODE_FRAGMENT[..n].to_string()),
        1 => (1_000usize..8_000).prop_map(|n| "x".repeat(n)),
        2 => code_like_source(),
        2 => prose_like_text(),
    ]
}

// The dispatcher must never panic on arbitrary text, however
// pathological, and must be deterministic.
//
// The two richer generator buckets (real,
// varied-structure source code and prose, both large enough to reach a
// dispatch arm) make each case noticeably more expensive than the old
// all-small-or-threshold-capped mix — a meaningful share of cases now
// actually run CodeAwareCompressor's tree-sitter parse or the Kompress
// path instead of bottoming out at `BelowByteThreshold` immediately.
// Case counts were cut from the earlier 2048/1024 (order of magnitude
// of `sse_framing.rs`'s parser fuzz tests) down to 40/20 after two
// larger settings both measured over the ~60s guidance with the richer
// generators in place: 128/64 measured ~84s combined, 64/32 measured
// ~60s combined (right at the boundary, not comfortably under it).
// 40/20 is what actually landed with margin. If a future
// change to these generators pushes wall time back over budget, the
// guidance is to cut case counts further, not shrink the payload
// ranges back down to threshold-capped sizes (that would silently
// regress dispatch reach). The no-panic property keeps the larger count
// since it's the property most likely to catch a real crash, the
// determinism property the smaller since each case dispatches twice.
//
// Kompress may be cache-cold on this machine (the HF cache is
// per-machine, not part of the repo) — both properties below must
// hold regardless: `kompress_or_noop` degrades to a deterministic
// NoOp when the model isn't cache-resident, which is a valid outcome
// for both "never panics" and "deterministic", not a test dependency
// on the model being loaded.

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 40,
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
        cases: 20,
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

// ─── Part 1, instrumentation: measured dispatch-reach fractions ───────
//
// A review quantified (from pinned
// proptest 1.11.0 source and the earlier `pathological_text()`
// weights) that ~4.5% of generated cases reached ANY dispatch arm and
// 0% ever reached SourceCode. This section re-measures the same
// quantity against the CURRENT generator empirically, rather than just
// asserting the fix worked.

/// Mirror of the dispatcher's private per-content-type byte thresholds
/// (`live_zone.rs`'s `THRESHOLD_*` consts, read via `threshold_for`),
/// duplicated here because this instrumentation test needs to decide
/// "would this case have cleared the byte-threshold gate" without
/// reimplementing `compress_one_block` itself. Values: SourceCode 2048,
/// PlainText 5120, everything else 512 (all pinned in `live_zone.rs`).
/// If production's thresholds
/// ever change, this drifts and the printed/asserted fractions below
/// become stale — it cannot hide a panic or a wrong *classification*,
/// both of which are still independently caught by
/// `dispatch_no_panic_on_arbitrary_text` and by using the real
/// `detect_content_type` (not a reimplementation) for classification.
fn mirrored_threshold_for(content_type: ContentType) -> usize {
    match content_type {
        ContentType::SourceCode => 2_048,
        ContentType::PlainText => 5_120,
        ContentType::JsonArray
        | ContentType::SearchResults
        | ContentType::BuildOutput
        | ContentType::GitDiff
        | ContentType::Html => 512,
    }
}

/// Sample count for the reach-fraction measurement below. Classification
/// runs through `detect_content_type` only — no compressor is ever
/// invoked — so this stays well under a second even at this size.
const REACH_SAMPLE_COUNT: u32 = 5_000;

/// Measures, over `REACH_SAMPLE_COUNT` samples of `pathological_text()`,
/// what fraction actually clear their content type's byte threshold and
/// would reach a `dispatch_compressor` arm (as opposed to being filtered
/// out at `BelowByteThreshold` before any compressor runs), broken down
/// by content type. Classification uses the SAME public function the
/// dispatcher itself calls (`headroom_core::transforms::detect_content_type`,
/// invoked from `live_zone.rs` right before `compress_one_block`'s
/// threshold gate — see that call site), on the same raw content-text
/// string this file's own `body_with_tool_result` embeds, so this
/// measures the real thing rather than a guess.
///
/// Uses a fixed-seed `TestRunner` (not the `proptest!` macro) so the
/// sample is reproducible run to run — this test's job is to produce a
/// stable, reportable number, not to hunt for new failing cases (that's
/// what the two properties above are for).
///
/// Floors are set well below the closed-form expectation from the
/// current bucket weights (SourceCode ~20%, PlainText ~24%, overall
/// ~44% — from each bucket's weight times its probability of clearing
/// its threshold) so ordinary sampling noise
/// at N=5000 can't flake this, while a future regression that collapses
/// a bucket back to threshold-capped output (the original
/// failure mode this instrumentation exists to catch) fails loudly
/// instead of silently.
#[test]
fn dispatch_reach_fractions_meet_floor() {
    let mut runner = TestRunner::new_with_rng(
        ProptestConfig::default(),
        TestRng::from_seed(RngAlgorithm::ChaCha, &[0x5A; 32]),
    );
    let strategy = pathological_text();

    let mut reached_total = 0u32;
    let mut reached_source_code = 0u32;
    let mut reached_plain_text = 0u32;
    let mut sampled = 0u32;

    for _ in 0..REACH_SAMPLE_COUNT {
        let tree = strategy
            .new_tree(&mut runner)
            .expect("pathological_text() strategy never rejects a case");
        let text = tree.current();
        if text.is_empty() {
            // `dispatch_compressor` special-cases empty content to an
            // unconditional NoOp before the byte-threshold gate even
            // runs (see live_zone.rs) — "reach" is undefined for it.
            // Vanishingly rare from these generators; excluded from
            // both numerator and denominator rather than counted
            // either way.
            continue;
        }
        sampled += 1;
        let detected = detect_content_type(&text);
        let reached = text.len() >= mirrored_threshold_for(detected.content_type);
        if reached {
            reached_total += 1;
            match detected.content_type {
                ContentType::SourceCode => reached_source_code += 1,
                ContentType::PlainText => reached_plain_text += 1,
                _ => {}
            }
        }
    }

    let denom = f64::from(sampled.max(1));
    let overall_frac = f64::from(reached_total) / denom;
    let source_frac = f64::from(reached_source_code) / denom;
    let plain_frac = f64::from(reached_plain_text) / denom;

    println!(
        "dispatch-reach over {sampled} sampled cases (of {REACH_SAMPLE_COUNT} drawn): \
         overall {overall_frac:.4} ({reached_total}), \
         source_code {source_frac:.4} ({reached_source_code}), \
         plain_text {plain_frac:.4} ({reached_plain_text})"
    );

    assert!(
        source_frac > 0.10,
        "SourceCode dispatch-reach fraction regressed: {source_frac:.4} (want > 0.10)"
    );
    assert!(
        plain_frac > 0.10,
        "PlainText dispatch-reach fraction regressed: {plain_frac:.4} (want > 0.10)"
    );
    assert!(
        overall_frac > 0.25,
        "overall dispatch-reach fraction regressed: {overall_frac:.4} (want > 0.25)"
    );
}
