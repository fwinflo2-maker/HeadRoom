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
//! Property-test coverage for the dispatch arms themselves (case
//! counts, no-panic / determinism / byte-fidelity properties) lands in
//! a follow-up commit in this same file.

use headroom_core::transforms::live_zone::content_type_from_name;
use headroom_core::transforms::ContentType;

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
