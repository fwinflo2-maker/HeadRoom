//! Per-token USD pricing, used to value spend and savings.
//!
//! Reads the LiteLLM price table already vendored for
//! [`crate::compression::model_limits`] — one embedded copy, two
//! consumers. No new data file, and no startup network dependency:
//! the table ships inside the binary and parses lazily on first
//! lookup.
//!
//! # Lookup discipline
//!
//! Lookup is exact-match over a small, deterministic candidate list
//! derived from the request's model id (lowercased, then: geo prefix
//! stripped, `:rev` suffix trimmed, provider path segment dropped).
//! We deliberately do NOT substring-scan the table: a short or
//! generic id must miss (and be logged) rather than misprice against
//! whichever stored id happens to contain it.
//!
//! **The unmodified id is always tried first**, because Bedrock
//! cross-region pricing is genuinely region-specific — e.g.
//! `eu.anthropic.claude-3-5-haiku-20241022-v1:0` bills at $0.25/M
//! against `anthropic.claude-3-5-haiku-20241022-v1:0`'s $0.80/M. Any
//! region the table tracks therefore gets its true price. The
//! geo-stripped candidate is only a *fallback* for a region the
//! table does not list (e.g. `apac.` today), where the base price is
//! a far better estimate than $0 — approximate, but never silently
//! preferred over an exact regional entry.
//!
//! # Coverage
//!
//! The vendored snapshot tracks current model generations plus every
//! Bedrock/Vertex-prefixed id, but it does not carry every legacy
//! direct-API id (`claude-3-5-sonnet-20241022`, for one, is absent
//! while its Bedrock form is present). Those price at $0 with a WARN
//! until the table is refreshed — see the module note below.
//!
//! # Unknown models price at $0
//!
//! A miss returns `None`; the ledger then records $0 for every USD
//! field and this module logs one WARN naming the model. We
//! deliberately do NOT substitute a blended fallback rate: phantom
//! dollars on a cost dashboard are worse than an obvious zero, and
//! the WARN tells an operator exactly which id to add by refreshing
//! the vendored table (`scripts/refresh_model_limits.sh`).

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use crate::bedrock::vendor::strip_geo_prefix;
use crate::compression::model_limits::VENDORED_JSON;

/// Per-token USD prices for one model. All fields are $/token
/// (LiteLLM stores them that way natively — no per-million
/// conversion happens here).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ModelPrice {
    pub input: f64,
    pub output: f64,
    pub cache_read: f64,
    pub cache_write: f64,
}

impl ModelPrice {
    /// USD saved by removing `tokens_saved` input tokens before they
    /// were sent (compression savings).
    pub fn compression_savings_usd(&self, tokens_saved: u64) -> f64 {
        tokens_saved as f64 * self.input
    }

    /// USD saved by cache reads: the discount delta vs list input
    /// price. A negative discount (cache reads priced above input —
    /// wire-format pathology) clamps to $0 rather than accruing
    /// negative savings.
    pub fn cache_savings_usd(&self, cache_read_tokens: u64) -> f64 {
        let discount = self.input - self.cache_read;
        if discount <= 0.0 {
            return 0.0;
        }
        cache_read_tokens as f64 * discount
    }

    /// USD actually spent on input for a request, using the cache
    /// breakdown when any segment is non-zero (never adding the
    /// total on top of the breakdown — that would double-count).
    pub fn input_cost_usd(
        &self,
        uncached_input_tokens: u64,
        cache_read_tokens: u64,
        cache_write_tokens: u64,
    ) -> f64 {
        uncached_input_tokens as f64 * self.input
            + cache_read_tokens as f64 * self.cache_read
            + cache_write_tokens as f64 * self.cache_write
    }
}

static BOOK: OnceLock<HashMap<String, ModelPrice>> = OnceLock::new();

/// Bounded once-per-model warn set so an unknown model logs one WARN,
/// not one per request. Capped so attacker-controlled model ids can't
/// grow it without bound; once full, further unknown models simply
/// stop logging (they still price at $0).
static WARNED: OnceLock<Mutex<std::collections::HashSet<String>>> = OnceLock::new();
const WARNED_CAP: usize = 256;

fn book() -> &'static HashMap<String, ModelPrice> {
    BOOK.get_or_init(parse_vendored)
}

fn parse_vendored() -> HashMap<String, ModelPrice> {
    let raw: serde_json::Value = serde_json::from_str(VENDORED_JSON)
        .expect("vendored LiteLLM JSON must parse — same invariant as model_limits");
    let mut out = HashMap::new();
    let Some(obj) = raw.as_object() else {
        return out;
    };
    for (model_id, spec) in obj {
        if model_id == "sample_spec" {
            continue;
        }
        let Some(spec) = spec.as_object() else {
            continue;
        };
        let input = spec.get("input_cost_per_token").and_then(|v| v.as_f64());
        let output = spec.get("output_cost_per_token").and_then(|v| v.as_f64());
        let (Some(input), Some(output)) = (input, output) else {
            continue;
        };
        if !input.is_finite() || !output.is_finite() || input < 0.0 || output < 0.0 {
            continue;
        }
        let cache_read = spec
            .get("cache_read_input_token_cost")
            .and_then(|v| v.as_f64())
            .filter(|c| c.is_finite() && *c >= 0.0)
            .unwrap_or(input);
        let cache_write = spec
            .get("cache_creation_input_token_cost")
            .and_then(|v| v.as_f64())
            .filter(|c| c.is_finite() && *c >= 0.0)
            .unwrap_or(input);
        out.insert(
            model_id.to_ascii_lowercase(),
            ModelPrice {
                input,
                output,
                cache_read,
                cache_write,
            },
        );
    }
    out
}

/// Candidate keys tried, in order, for a request model id. Exact
/// matches only — see module doc for why there is no substring scan.
fn candidates(model: &str) -> Vec<String> {
    let lower = model.trim().to_ascii_lowercase();
    let mut out = Vec::with_capacity(6);
    let mut push = |s: String| {
        if !s.is_empty() && !out.contains(&s) {
            out.push(s);
        }
    };
    push(lower.clone());
    // Bedrock cross-region profile: `eu.anthropic.claude…` prices as
    // `anthropic.claude…`.
    let geo = strip_geo_prefix(&lower).to_string();
    push(geo.clone());
    // Bedrock revision suffix: `…-v1:0` is stored both with and
    // without `:0` across providers; try the trimmed form.
    if let Some((head, _)) = geo.split_once(':') {
        push(head.to_string());
    }
    if let Some((head, _)) = lower.split_once(':') {
        push(head.to_string());
    }
    // Provider-routed ids (`openai/gpt-4o`, `github-copilot/claude…`):
    // the bare tail is how LiteLLM stores first-party models.
    if let Some((_, tail)) = lower.rsplit_once('/') {
        push(tail.to_string());
    }
    out
}

/// Look up per-token pricing for a model id. `None` means "not in
/// the vendored table" — the caller records $0 for USD fields and
/// this module logs one WARN per distinct model id.
pub fn lookup(model: &str) -> Option<ModelPrice> {
    let table = book();
    for key in candidates(model) {
        if let Some(p) = table.get(&key) {
            return Some(*p);
        }
    }
    warn_once(model);
    None
}

fn warn_once(model: &str) {
    let set = WARNED.get_or_init(|| Mutex::new(std::collections::HashSet::new()));
    let mut set = set
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if set.len() >= WARNED_CAP || !set.insert(model.to_string()) {
        return;
    }
    tracing::warn!(
        event = "savings_price_unknown_model",
        model = %model,
        "model id not in the vendored price table; USD savings for it \
         record as $0 — refresh data/model_prices_and_context_window.json \
         via scripts/refresh_model_limits.sh to add it"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_anthropic_direct_id_prices() {
        let p = lookup("claude-sonnet-4-5-20250929").expect("priced");
        assert!(p.input > 0.0 && p.output > p.input);
        assert!(p.cache_read < p.input, "cache reads are discounted");
        assert!(p.cache_write > p.input, "cache writes carry a premium");
    }

    /// Bedrock cross-region pricing is region-specific, so an exact
    /// regional entry must win over the geo-stripped base id.
    #[test]
    fn exact_regional_bedrock_entry_wins_over_base() {
        let base = lookup("anthropic.claude-3-5-haiku-20241022-v1:0").expect("base priced");
        let eu = lookup("eu.anthropic.claude-3-5-haiku-20241022-v1:0").expect("eu priced");
        assert!(
            (base.input - 8e-7).abs() < 1e-12,
            "base id prices at list rate, got {}",
            base.input
        );
        assert!(
            (eu.input - 2.5e-7).abs() < 1e-12,
            "eu id must use its own regional rate, got {}",
            eu.input
        );
        assert_ne!(
            base, eu,
            "geo-stripping must not clobber a tracked regional price"
        );
    }

    /// A region the table does NOT list falls back to the base id —
    /// approximate, but far better than $0.
    #[test]
    fn untracked_region_falls_back_to_base_price() {
        assert!(
            book()
                .get("apac.anthropic.claude-3-5-haiku-20241022-v1:0")
                .is_none(),
            "fixture guard: this id must be absent for the fallback to be under test"
        );
        let base = lookup("anthropic.claude-3-5-haiku-20241022-v1:0").expect("base priced");
        let apac = lookup("apac.anthropic.claude-3-5-haiku-20241022-v1:0").expect("falls back");
        assert_eq!(base, apac, "unlisted region falls back to the base entry");
    }

    #[test]
    fn provider_path_segment_falls_back_to_bare_tail() {
        let bare = lookup("gpt-4o").expect("bare id priced");
        let routed = lookup("openai/gpt-4o").expect("routed id priced");
        assert_eq!(bare, routed);
    }

    #[test]
    fn unknown_model_misses_instead_of_mispricing() {
        assert!(lookup("claude").is_none(), "generic id must not match");
        assert!(lookup("totally-unknown-model-xyz").is_none());
        assert!(lookup("").is_none());
    }

    #[test]
    fn case_insensitive_lookup() {
        assert_eq!(lookup("GPT-4o"), lookup("gpt-4o"));
    }

    #[test]
    fn usd_helpers_compute_list_price_math() {
        let p = ModelPrice {
            input: 3e-6,
            output: 15e-6,
            cache_read: 3e-7,
            cache_write: 3.75e-6,
        };
        assert!((p.compression_savings_usd(1_000_000) - 3.0).abs() < 1e-9);
        // Cache savings = discount delta vs list input price.
        assert!((p.cache_savings_usd(1_000_000) - 2.7).abs() < 1e-9);
        // Breakdown input cost: uncached + cache_read + cache_write.
        let usd = p.input_cost_usd(1_000_000, 1_000_000, 1_000_000);
        assert!((usd - (3.0 + 0.3 + 3.75)).abs() < 1e-9);
        // Inverted cache pricing clamps to $0, never negative savings.
        let inverted = ModelPrice {
            input: 1e-6,
            output: 1e-6,
            cache_read: 2e-6,
            cache_write: 1e-6,
        };
        assert_eq!(inverted.cache_savings_usd(1000), 0.0);
    }
}
