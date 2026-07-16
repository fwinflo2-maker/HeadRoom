//! Native savings ledger — request-level savings/cost telemetry for
//! the Rust proxy, served by `/stats`, `/stats/timeseries`,
//! `/stats/events`, and the `/dashboard` page.
//!
//! # Design
//!
//! Everything is **one fold, many views**: each finished request
//! becomes a [`RequestEvent`], and every aggregate the proxy exposes
//! — lifetime, since-boot session, per-provider, per-model, hourly
//! and daily buckets — is the same [`Totals::fold`] applied under a
//! different key. There is exactly one place where accounting
//! happens, so the views cannot disagree with each other.
//!
//! Persisted state is the aggregate map itself (schema `version: 1`,
//! this module's own shape — deliberately *not* the Python tracker's
//! file format): lifetime totals plus bounded per-model, per-day
//! (~400 days) and per-hour (48 h) buckets. At ~200 bytes a row the
//! file tops out around 100 KB, so persistence is a single atomic
//! snapshot write (temp → fsync → rename → fsync dir) on a
//! background interval — no write-ahead log, no compaction machinery.
//!
//! # Hot-path contract
//!
//! [`Ledger::record`] performs **no disk I/O and no await**: fold
//! under a mutex, set a dirty flag, return. A background task
//! ([`spawn_flusher`]) and the shutdown path both call
//! [`Ledger::flush`], which serialises writers behind an async gate
//! and does filesystem work on the blocking pool. A failed write
//! re-arms the dirty flag so transient errors retry.
//!
//! # Honesty rules
//!
//! - A failed upstream (non-2xx / transport error) counts in
//!   `requests`/`failed` but accrues **no** tokens, USD, or bucket
//!   rows — retries can never inflate savings.
//! - Unknown models value at $0 with one WARN
//!   (see [`super::pricing`]); no phantom dollars.
//! - Counter math saturates; USD accumulators reject non-finite or
//!   negative increments; loading clamps hand-edited negatives to 0.
//! - `per_model` keys come from request bodies (attacker-
//!   controlled): length-clamped, cardinality-capped, overflow folds
//!   into `"other"`.

use std::collections::{BTreeMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::pricing;

/// Persisted-file schema version (this module's own, v1).
const STATS_SCHEMA: u64 = 1;
/// Hourly buckets kept (48 h of hour-resolution history).
const MAX_HOUR_ROWS: usize = 48;
/// Daily buckets kept (~13 months of day-resolution history).
const MAX_DAY_ROWS: usize = 400;
/// Bounded per-model cardinality; overflow folds into [`OVERFLOW_KEY`].
const MAX_MODEL_ROWS: usize = 100;
const OVERFLOW_KEY: &str = "other";
/// Model id used until a lane supplies a real one.
const UNKNOWN_MODEL: &str = "unknown";
/// Recent-request ring served by `/stats/events`. Memory only.
const RECENT_CAP: usize = 100;
/// Length clamp for client-controlled strings (model / request ids).
const MAX_FIELD_LEN: usize = 120;
/// Background flush cadence.
// ponytail: fixed 10s flush; promote to a flag only if a deployment
// actually needs to tune it.
pub const FLUSH_INTERVAL: Duration = Duration::from_secs(10);

// ───────────────────────── inputs ─────────────────────────

/// Provider lane tags — `&'static str` by construction, so the
/// per-provider map is bounded without runtime checks.
pub mod provider {
    pub const ANTHROPIC: &str = "anthropic";
    pub const OPENAI_CHAT: &str = "openai";
    pub const OPENAI_RESPONSES: &str = "openai_responses";
    pub const BEDROCK: &str = "bedrock";
    pub const VERTEX: &str = "vertex";
}

/// Token usage from an upstream response, normalised to Anthropic
/// semantics: `input_tokens` is the *uncached* input portion; cache
/// reads/writes are separate. (OpenAI's `prompt_tokens` includes
/// cached tokens — capture code subtracts before building this.)
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
}

impl Usage {
    /// Full prompt context that reached the model.
    pub fn total_input(&self) -> u64 {
        self.input_tokens
            .saturating_add(self.cache_read_tokens)
            .saturating_add(self.cache_write_tokens)
    }
}

/// One finished request, ready to fold into the ledger.
#[derive(Debug, Clone)]
pub struct RequestEvent {
    pub provider: &'static str,
    pub model: String,
    pub request_id: String,
    /// Input-token estimate before/after compression from the
    /// dispatcher; zero when the request wasn't tokenized.
    pub tokens_before: u64,
    pub tokens_after: u64,
    pub usage: Usage,
    pub failed: bool,
    pub latency_ms: u64,
    /// Compression strategies applied (feed detail).
    pub transforms: Vec<String>,
}

impl RequestEvent {
    /// Single construction point — clamps client-controlled strings.
    pub fn new(provider: &'static str, model: &str, request_id: &str) -> Self {
        let model = model.trim();
        Self {
            provider,
            model: clamp_field(if model.is_empty() {
                UNKNOWN_MODEL
            } else {
                model
            }),
            request_id: clamp_field(request_id),
            tokens_before: 0,
            tokens_after: 0,
            usage: Usage::default(),
            failed: false,
            latency_ms: 0,
            transforms: Vec::new(),
        }
    }

    /// Replace the model id, clamped through the same single point
    /// as [`RequestEvent::new`]. Empty input is ignored.
    ///
    /// Recording is deliberately NOT gated on the compression
    /// buffer (compression is off by default), so on a pure
    /// passthrough request the model is only learned from the
    /// response — see [`super::capture`].
    pub fn set_model(&mut self, model: &str) {
        let model = model.trim();
        if !model.is_empty() {
            self.model = clamp_field(model);
        }
    }

    /// True while no lane has supplied a real model id.
    pub fn model_is_unknown(&self) -> bool {
        self.model == UNKNOWN_MODEL
    }

    pub fn tokens_saved(&self) -> u64 {
        self.tokens_before.saturating_sub(self.tokens_after)
    }
}

// ───────────────────── the one accumulator ─────────────────────

/// The single accumulator every view is built from. `fold` is the
/// only place request accounting happens.
#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq)]
pub struct Totals {
    #[serde(default)]
    pub requests: u64,
    #[serde(default)]
    pub failed: u64,
    /// Requests where compression removed at least one token.
    #[serde(default)]
    pub compressed: u64,
    #[serde(default)]
    pub tokens_before: u64,
    #[serde(default)]
    pub tokens_after: u64,
    #[serde(default)]
    pub tokens_saved: u64,
    #[serde(default)]
    pub input_tokens: u64,
    #[serde(default)]
    pub output_tokens: u64,
    #[serde(default)]
    pub cache_read_tokens: u64,
    #[serde(default)]
    pub cache_write_tokens: u64,
    #[serde(default)]
    pub compression_savings_usd: f64,
    #[serde(default)]
    pub cache_savings_usd: f64,
    #[serde(default)]
    pub input_cost_usd: f64,
    #[serde(default)]
    pub latency_sum_ms: u64,
    #[serde(default)]
    pub latency_count: u64,
}

/// USD values computed once per event (so every fold target gets
/// identical dollars).
#[derive(Debug, Default, Clone, Copy)]
struct EventUsd {
    compression: f64,
    cache: f64,
    input_cost: f64,
}

impl Totals {
    fn fold(&mut self, ev: &RequestEvent, usd: EventUsd) {
        self.requests = self.requests.saturating_add(1);
        if ev.latency_ms > 0 {
            self.latency_sum_ms = self.latency_sum_ms.saturating_add(ev.latency_ms);
            self.latency_count = self.latency_count.saturating_add(1);
        }
        if ev.failed {
            // Failures count — and accrue nothing else.
            self.failed = self.failed.saturating_add(1);
            return;
        }
        let saved = ev.tokens_saved();
        if saved > 0 {
            self.compressed = self.compressed.saturating_add(1);
        }
        self.tokens_before = self.tokens_before.saturating_add(ev.tokens_before);
        self.tokens_after = self.tokens_after.saturating_add(ev.tokens_after);
        self.tokens_saved = self.tokens_saved.saturating_add(saved);
        self.input_tokens = self.input_tokens.saturating_add(ev.usage.input_tokens);
        self.output_tokens = self.output_tokens.saturating_add(ev.usage.output_tokens);
        self.cache_read_tokens = self
            .cache_read_tokens
            .saturating_add(ev.usage.cache_read_tokens);
        self.cache_write_tokens = self
            .cache_write_tokens
            .saturating_add(ev.usage.cache_write_tokens);
        accumulate_usd(&mut self.compression_savings_usd, usd.compression);
        accumulate_usd(&mut self.cache_savings_usd, usd.cache);
        accumulate_usd(&mut self.input_cost_usd, usd.input_cost);
    }

    /// Clamp USD fields loaded from disk (hand-edits, corruption).
    fn sanitize(&mut self) {
        for v in [
            &mut self.compression_savings_usd,
            &mut self.cache_savings_usd,
            &mut self.input_cost_usd,
        ] {
            if !v.is_finite() || *v < 0.0 {
                *v = 0.0;
            }
        }
    }

    pub fn savings_usd(&self) -> f64 {
        self.compression_savings_usd + self.cache_savings_usd
    }

    /// Effective input volume the operator would have paid for
    /// without compression.
    fn would_be_input(&self) -> u64 {
        self.input_tokens
            .saturating_add(self.cache_read_tokens)
            .saturating_add(self.cache_write_tokens)
            .saturating_add(self.tokens_saved)
    }

    /// JSON view. One serializer for every scope, plus derived
    /// ratios the dashboard shows.
    fn view(&self) -> Value {
        let mut v = serde_json::to_value(self).unwrap_or_else(|_| json!({}));
        if let Some(obj) = v.as_object_mut() {
            obj.insert("savings_usd".into(), json!(self.savings_usd()));
            let would_be = self.would_be_input();
            obj.insert(
                "savings_percent".into(),
                json!(percent(self.tokens_saved, would_be)),
            );
            obj.insert(
                "compression_percent".into(),
                json!(percent(self.tokens_saved, self.tokens_before)),
            );
            obj.insert(
                "cache_hit_percent".into(),
                json!(percent(
                    self.cache_read_tokens,
                    self.cache_read_tokens
                        .saturating_add(self.input_tokens)
                        .saturating_add(self.cache_write_tokens),
                )),
            );
            obj.insert(
                "average_latency_ms".into(),
                json!(if self.latency_count > 0 {
                    self.latency_sum_ms as f64 / self.latency_count as f64
                } else {
                    0.0
                }),
            );
        }
        v
    }
}

fn percent(part: u64, whole: u64) -> f64 {
    if whole == 0 {
        0.0
    } else {
        (part as f64 / whole as f64) * 100.0
    }
}

// ───────────────────── persisted state (v1) ─────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedStats {
    /// Discriminator + version for future migrations.
    format: String,
    version: u64,
    /// First time this ledger ever saw traffic.
    #[serde(default)]
    first_seen_at: Option<String>,
    #[serde(default)]
    lifetime: Totals,
    #[serde(default)]
    per_provider: BTreeMap<String, Totals>,
    #[serde(default)]
    per_model: BTreeMap<String, Totals>,
    /// `YYYY-MM-DD` → totals, bounded to [`MAX_DAY_ROWS`].
    #[serde(default)]
    days: BTreeMap<String, Totals>,
    /// `YYYY-MM-DDTHH` → totals, bounded to [`MAX_HOUR_ROWS`].
    #[serde(default)]
    hours: BTreeMap<String, Totals>,
}

impl Default for PersistedStats {
    fn default() -> Self {
        Self {
            format: "headroom-native-stats".into(),
            version: STATS_SCHEMA,
            first_seen_at: None,
            lifetime: Totals::default(),
            per_provider: BTreeMap::new(),
            per_model: BTreeMap::new(),
            days: BTreeMap::new(),
            hours: BTreeMap::new(),
        }
    }
}

impl PersistedStats {
    fn sanitize(&mut self) {
        self.format = "headroom-native-stats".into();
        self.version = STATS_SCHEMA;
        self.lifetime.sanitize();
        for t in self.per_provider.values_mut() {
            t.sanitize();
        }
        for t in self.per_model.values_mut() {
            t.sanitize();
        }
        for t in self.days.values_mut() {
            t.sanitize();
        }
        for t in self.hours.values_mut() {
            t.sanitize();
        }
        trim_oldest(&mut self.days, MAX_DAY_ROWS);
        trim_oldest(&mut self.hours, MAX_HOUR_ROWS);
        while self.per_model.len() > MAX_MODEL_ROWS + 1 {
            let key = self
                .per_model
                .keys()
                .find(|k| k.as_str() != OVERFLOW_KEY)
                .cloned();
            match key {
                Some(k) => {
                    self.per_model.remove(&k);
                }
                None => break,
            }
        }
    }
}

/// Drop oldest keys until `map` holds at most `cap` rows. BTreeMap
/// keys are date-prefixed labels, so "first" is "oldest".
fn trim_oldest(map: &mut BTreeMap<String, Totals>, cap: usize) {
    while map.len() > cap {
        let Some(k) = map.keys().next().cloned() else {
            break;
        };
        map.remove(&k);
    }
}

// ───────────────────── recent ring (memory only) ─────────────────────

#[derive(Debug, Clone, Serialize)]
struct RecentRow {
    request_id: String,
    timestamp: String,
    provider: &'static str,
    model: String,
    tokens_before: u64,
    tokens_after: u64,
    tokens_saved: u64,
    input_tokens: u64,
    output_tokens: u64,
    cache_read_tokens: u64,
    cache_write_tokens: u64,
    compression_savings_usd: f64,
    cache_savings_usd: f64,
    savings_percent: f64,
    latency_ms: u64,
    failed: bool,
    transforms: Vec<String>,
}

// ───────────────────────── the ledger ─────────────────────────

struct Inner {
    persisted: PersistedStats,
    session: Totals,
    session_per_provider: BTreeMap<&'static str, Totals>,
    recent: VecDeque<RecentRow>,
}

pub struct Ledger {
    inner: Mutex<Inner>,
    dirty: AtomicBool,
    path: Option<PathBuf>,
    /// Serialises the background flusher against the shutdown flush.
    flush_gate: tokio::sync::Mutex<()>,
    persistence_error: Mutex<Option<String>>,
    started_at: SystemTime,
}

impl std::fmt::Debug for Ledger {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Ledger")
            .field("path", &self.path)
            .finish_non_exhaustive()
    }
}

impl Ledger {
    /// In-memory ledger (tests, or no usable state dir). Never
    /// touches disk.
    pub fn in_memory() -> Self {
        Self::from_state(PersistedStats::default(), None, None)
    }

    /// Load (or initialise) the ledger at `path`. A corrupt file is
    /// moved aside (`<path>.corrupt-<secs>`) and the ledger starts
    /// fresh — loudly, never silently zeroing an intact file.
    pub fn load(path: PathBuf) -> Self {
        let (state, error) = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<PersistedStats>(&bytes) {
                Ok(mut s) => {
                    s.sanitize();
                    (s, None)
                }
                Err(e) => {
                    let aside = path.with_extension(format!("corrupt-{}", epoch_secs()));
                    let moved = std::fs::rename(&path, &aside).is_ok();
                    tracing::error!(
                        event = "stats_state_corrupt",
                        path = %path.display(),
                        preserved = moved,
                        aside = %aside.display(),
                        error = %e,
                        "persisted stats failed to parse; starting fresh"
                    );
                    (PersistedStats::default(), Some(e.to_string()))
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (PersistedStats::default(), None),
            Err(e) => {
                tracing::error!(
                    event = "stats_state_unreadable",
                    path = %path.display(),
                    error = %e,
                    "persisted stats unreadable; starting fresh (writes will retry)"
                );
                (PersistedStats::default(), Some(e.to_string()))
            }
        };
        Self::from_state(state, Some(path), error)
    }

    fn from_state(persisted: PersistedStats, path: Option<PathBuf>, error: Option<String>) -> Self {
        Self {
            inner: Mutex::new(Inner {
                persisted,
                session: Totals::default(),
                session_per_provider: BTreeMap::new(),
                recent: VecDeque::with_capacity(RECENT_CAP),
            }),
            dirty: AtomicBool::new(false),
            path,
            flush_gate: tokio::sync::Mutex::new(()),
            persistence_error: Mutex::new(error),
            started_at: SystemTime::now(),
        }
    }

    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        // A panic while the lock is held must not take recording or
        // /stats down for the rest of the process.
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    // ───────────────────── record ─────────────────────

    /// Fold one finished request into every view. **No I/O, no await.**
    pub fn record(&self, ev: RequestEvent) {
        let at = SystemTime::now();
        // Price once; every fold target gets identical dollars.
        // Failed requests skip pricing entirely — fold() ignores the
        // numbers anyway, and a hostile model id on a failed request
        // shouldn't cost a table lookup + warn.
        let usd = if ev.failed {
            EventUsd::default()
        } else {
            match pricing::lookup(&ev.model) {
                Some(p) => EventUsd {
                    compression: p.compression_savings_usd(ev.tokens_saved()),
                    cache: p.cache_savings_usd(ev.usage.cache_read_tokens),
                    input_cost: p.input_cost_usd(
                        // When the response carried no usage block,
                        // bill the post-compression estimate instead
                        // so passthrough lanes still show spend.
                        if ev.usage.total_input() > 0 {
                            ev.usage.input_tokens
                        } else {
                            ev.tokens_after
                        },
                        ev.usage.cache_read_tokens,
                        ev.usage.cache_write_tokens,
                    ),
                },
                None => EventUsd::default(),
            }
        };

        let iso = to_iso(at);
        let day_key = iso[..10].to_string(); // YYYY-MM-DD
        let hour_key = iso[..13].to_string(); // YYYY-MM-DDTHH

        let mut inner = self.lock();
        if inner.persisted.first_seen_at.is_none() {
            inner.persisted.first_seen_at = Some(iso.clone());
        }

        inner.session.fold(&ev, usd);
        inner
            .session_per_provider
            .entry(ev.provider)
            .or_default()
            .fold(&ev, usd);

        let p = &mut inner.persisted;
        p.lifetime.fold(&ev, usd);
        p.per_provider
            .entry(ev.provider.to_string())
            .or_default()
            .fold(&ev, usd);
        let model_key =
            if p.per_model.len() >= MAX_MODEL_ROWS && !p.per_model.contains_key(&ev.model) {
                OVERFLOW_KEY.to_string()
            } else {
                ev.model.clone()
            };
        p.per_model.entry(model_key).or_default().fold(&ev, usd);
        p.days.entry(day_key).or_default().fold(&ev, usd);
        p.hours.entry(hour_key).or_default().fold(&ev, usd);
        trim_oldest(&mut p.days, MAX_DAY_ROWS);
        trim_oldest(&mut p.hours, MAX_HOUR_ROWS);

        // Recent ring — failures included (flagged), savings zeroed
        // by fold() semantics above.
        if inner.recent.len() >= RECENT_CAP {
            inner.recent.pop_front();
        }
        let saved = if ev.failed { 0 } else { ev.tokens_saved() };
        let row = RecentRow {
            request_id: ev.request_id,
            timestamp: iso,
            provider: ev.provider,
            model: ev.model,
            tokens_before: ev.tokens_before,
            tokens_after: ev.tokens_after,
            tokens_saved: saved,
            input_tokens: ev.usage.input_tokens,
            output_tokens: ev.usage.output_tokens,
            cache_read_tokens: ev.usage.cache_read_tokens,
            cache_write_tokens: ev.usage.cache_write_tokens,
            compression_savings_usd: if ev.failed { 0.0 } else { usd.compression },
            cache_savings_usd: if ev.failed { 0.0 } else { usd.cache },
            savings_percent: percent(saved, ev.tokens_before),
            latency_ms: ev.latency_ms,
            failed: ev.failed,
            transforms: ev.transforms,
        };
        inner.recent.push_back(row);
        drop(inner);
        self.dirty.store(true, Ordering::Release);
    }

    // ───────────────────── views ─────────────────────

    /// `GET /stats` — the whole snapshot.
    ///
    /// `summary.api_requests`, `requests.{total,failed,cached}` and
    /// `tokens.{saved,proxy_compression_saved}` are a live external
    /// contract: headway's unified-stats layer reads exactly those
    /// keys from this endpoint today. Everything else is this
    /// module's own schema.
    pub fn stats_payload(&self) -> Value {
        let inner = self.lock();
        let s = &inner.session;
        let session_providers: serde_json::Map<String, Value> = inner
            .session_per_provider
            .iter()
            .map(|(k, t)| (k.to_string(), t.view()))
            .collect();
        let lifetime_models: serde_json::Map<String, Value> = inner
            .persisted
            .per_model
            .iter()
            .map(|(k, t)| (k.clone(), t.view()))
            .collect();
        let lifetime_providers: serde_json::Map<String, Value> = inner
            .persisted
            .per_provider
            .iter()
            .map(|(k, t)| (k.clone(), t.view()))
            .collect();
        let recent: Vec<Value> = inner
            .recent
            .iter()
            .rev()
            .take(10)
            .map(|r| serde_json::to_value(r).unwrap_or(Value::Null))
            .collect();
        let healthy = self
            .persistence_error
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();

        json!({
            "proxy": "headroom-rust",
            "version": env!("CARGO_PKG_VERSION"),
            "started_at": to_iso(self.started_at),
            "uptime_seconds": self
                .started_at
                .elapsed()
                .map(|d| d.as_secs())
                .unwrap_or(0),
            "first_seen_at": inner.persisted.first_seen_at,
            // Live headway contract (see doc comment) — session scope.
            "summary": { "api_requests": s.requests },
            "requests": {
                "total": s.requests,
                "failed": s.failed,
                "compressed": s.compressed,
                "cached": 0,
            },
            "tokens": {
                "input": s.input_tokens,
                "output": s.output_tokens,
                "cache_read": s.cache_read_tokens,
                "cache_write": s.cache_write_tokens,
                "saved": s.tokens_saved,
                "proxy_compression_saved": s.tokens_saved,
            },
            // The real schema: the same Totals view at every scope.
            "session": s.view(),
            "session_by_provider": session_providers,
            "lifetime": inner.persisted.lifetime.view(),
            "lifetime_by_provider": lifetime_providers,
            "lifetime_by_model": lifetime_models,
            "recent_requests": recent,
            "persistence": {
                "enabled": self.path.is_some(),
                "path": self.path().map(|p| p.display().to_string()),
                "healthy": healthy.is_none(),
                "error": healthy,
                "dirty": self.dirty.load(Ordering::Acquire),
            },
        })
    }

    /// `GET /stats/timeseries?bucket=hour|day|week|month`.
    ///
    /// Hour rows cover the last 48 h; day rows ~13 months; week and
    /// month are folded from day rows at read time (weeks start
    /// Monday).
    pub fn timeseries_payload(&self, bucket: &str) -> Option<Value> {
        let inner = self.lock();
        let rows: Vec<(String, Totals)> = match bucket {
            "hour" => inner
                .persisted
                .hours
                .iter()
                .map(|(k, t)| (format!("{k}:00:00Z"), t.clone()))
                .collect(),
            "day" => inner
                .persisted
                .days
                .iter()
                .map(|(k, t)| (k.clone(), t.clone()))
                .collect(),
            "week" | "month" => {
                let mut folded: BTreeMap<String, Totals> = BTreeMap::new();
                for (day, t) in &inner.persisted.days {
                    let Some(label) = (if bucket == "month" {
                        day.get(..7).map(str::to_string)
                    } else {
                        monday_of(day)
                    }) else {
                        continue;
                    };
                    merge_totals(folded.entry(label).or_default(), t);
                }
                folded.into_iter().collect()
            }
            _ => return None,
        };
        let points: Vec<Value> = rows
            .into_iter()
            .map(|(label, t)| {
                let mut v = t.view();
                if let Some(obj) = v.as_object_mut() {
                    obj.insert("timestamp".into(), json!(label));
                }
                v
            })
            .collect();
        Some(json!({
            "bucket": bucket,
            "points": points,
            "retention": { "hours": MAX_HOUR_ROWS, "days": MAX_DAY_ROWS },
        }))
    }

    /// `GET /stats/events?limit=N` — newest first.
    pub fn events_payload(&self, limit: usize) -> Value {
        let inner = self.lock();
        let events: Vec<Value> = inner
            .recent
            .iter()
            .rev()
            .take(limit.clamp(1, RECENT_CAP))
            .map(|r| serde_json::to_value(r).unwrap_or(Value::Null))
            .collect();
        json!({ "events": events, "capacity": RECENT_CAP })
    }

    // ───────────────────── persistence ─────────────────────

    /// Persist dirty state as one atomic snapshot. Serialises
    /// callers behind an async gate; filesystem work runs on the
    /// blocking pool. A failed write logs, surfaces in
    /// `/stats.persistence`, and re-arms the dirty flag.
    pub async fn flush(self: &Arc<Self>) {
        let Some(path) = self.path.clone() else {
            return;
        };
        let _gate = self.flush_gate.lock().await;
        if !self.dirty.swap(false, Ordering::AcqRel) {
            return;
        }
        let payload = {
            let inner = self.lock();
            serde_json::to_vec_pretty(&inner.persisted)
        };
        let bytes = match payload {
            Ok(b) => b,
            Err(e) => {
                tracing::error!(
                    event = "stats_serialize_failed",
                    error = %e,
                    "stats state failed to serialize; will retry"
                );
                self.dirty.store(true, Ordering::Release);
                return;
            }
        };
        let write_path = path.clone();
        let result = tokio::task::spawn_blocking(move || write_atomic(&write_path, &bytes)).await;
        let flat = match result {
            Ok(inner) => inner,
            Err(join_err) => Err(std::io::Error::other(join_err)),
        };
        let mut err_slot = self
            .persistence_error
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        match flat {
            Ok(()) => *err_slot = None,
            Err(e) => {
                tracing::warn!(
                    event = "stats_flush_failed",
                    path = %path.display(),
                    error = %e,
                    "stats write failed; dirty flag re-armed for retry"
                );
                *err_slot = Some(e.to_string());
                self.dirty.store(true, Ordering::Release);
            }
        }
    }

    /// Test hook: unflushed state?
    pub fn is_dirty(&self) -> bool {
        self.dirty.load(Ordering::Acquire)
    }
}

/// Spawn the background flusher; the shutdown path calls `flush`
/// once more after the server drains.
pub fn spawn_flusher(ledger: Arc<Ledger>) {
    if ledger.path.is_none() {
        return;
    }
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(FLUSH_INTERVAL);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            ledger.flush().await;
        }
    });
}

// ───────────────────── helpers ─────────────────────

fn epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// RFC3339 UTC, seconds precision — `2026-07-16T08:12:34Z`.
fn to_iso(t: SystemTime) -> String {
    humantime::format_rfc3339_seconds(t).to_string()
}

/// Monday of the week containing a `YYYY-MM-DD` day label.
/// (1970-01-01 was a Thursday; `(days + 3) % 7 == 0` ⇔ Monday.)
fn monday_of(day: &str) -> Option<String> {
    let t = humantime::parse_rfc3339(&format!("{day}T00:00:00Z")).ok()?;
    let secs = t.duration_since(SystemTime::UNIX_EPOCH).ok()?.as_secs();
    let days = secs / 86_400;
    let monday = days - ((days + 3) % 7);
    let start = SystemTime::UNIX_EPOCH + Duration::from_secs(monday * 86_400);
    Some(to_iso(start)[..10].to_string())
}

fn merge_totals(into: &mut Totals, from: &Totals) {
    into.requests = into.requests.saturating_add(from.requests);
    into.failed = into.failed.saturating_add(from.failed);
    into.compressed = into.compressed.saturating_add(from.compressed);
    into.tokens_before = into.tokens_before.saturating_add(from.tokens_before);
    into.tokens_after = into.tokens_after.saturating_add(from.tokens_after);
    into.tokens_saved = into.tokens_saved.saturating_add(from.tokens_saved);
    into.input_tokens = into.input_tokens.saturating_add(from.input_tokens);
    into.output_tokens = into.output_tokens.saturating_add(from.output_tokens);
    into.cache_read_tokens = into
        .cache_read_tokens
        .saturating_add(from.cache_read_tokens);
    into.cache_write_tokens = into
        .cache_write_tokens
        .saturating_add(from.cache_write_tokens);
    accumulate_usd(
        &mut into.compression_savings_usd,
        from.compression_savings_usd,
    );
    accumulate_usd(&mut into.cache_savings_usd, from.cache_savings_usd);
    accumulate_usd(&mut into.input_cost_usd, from.input_cost_usd);
    into.latency_sum_ms = into.latency_sum_ms.saturating_add(from.latency_sum_ms);
    into.latency_count = into.latency_count.saturating_add(from.latency_count);
}

/// Clamp a client-controlled string on a char boundary.
fn clamp_field(s: &str) -> String {
    if s.len() <= MAX_FIELD_LEN {
        return s.to_string();
    }
    let mut end = MAX_FIELD_LEN;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
}

/// Fold a USD increment into an accumulator, rejecting NaN/inf and
/// negative increments, and repairing a bad base.
fn accumulate_usd(base: &mut f64, inc: f64) {
    if inc.is_finite() && inc > 0.0 {
        *base += inc;
    }
    if !base.is_finite() || *base < 0.0 {
        *base = 0.0;
    }
}

/// Atomic persist: unique temp file → fsync → rename → fsync parent
/// dir. Temp file removed on failure so retries don't litter.
fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let tmp = path.with_extension(format!("tmp-{}", std::process::id()));
    let result = (|| {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)?;
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                if let Ok(dir) = std::fs::File::open(parent) {
                    let _ = dir.sync_all();
                }
            }
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A model that is actually present in the vendored price table
    /// ($3/M in, $0.30/M cache read, $3.75/M cache write). Several
    /// legacy direct-API ids are absent from the snapshot, so tests
    /// asserting real USD must pin one that exists.
    const PRICED_MODEL: &str = "claude-sonnet-4-5-20250929";

    fn event(model: &str) -> RequestEvent {
        let mut ev = RequestEvent::new(provider::ANTHROPIC, model, "req-1");
        ev.tokens_before = 1000;
        ev.tokens_after = 600;
        ev.usage = Usage {
            input_tokens: 600,
            output_tokens: 50,
            cache_read_tokens: 200,
            cache_write_tokens: 100,
        };
        ev.latency_ms = 42;
        ev
    }

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("ledger-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    #[test]
    fn one_fold_updates_every_view_identically() {
        let ledger = Ledger::in_memory();
        ledger.record(event(PRICED_MODEL));
        let stats = ledger.stats_payload();
        // Session, lifetime, provider, and model views all saw the
        // same fold.
        for scope in [
            &stats["session"],
            &stats["lifetime"],
            &stats["session_by_provider"]["anthropic"],
            &stats["lifetime_by_provider"]["anthropic"],
            &stats["lifetime_by_model"][PRICED_MODEL],
        ] {
            assert_eq!(scope["requests"], 1, "scope: {scope}");
            assert_eq!(scope["tokens_saved"], 400);
            assert_eq!(scope["cache_read_tokens"], 200);
        }
        // 400 saved input tokens at $3/M.
        let comp = stats["session"]["compression_savings_usd"]
            .as_f64()
            .unwrap();
        assert!((comp - 0.0012).abs() < 1e-9, "comp={comp}");
        // 200 cache reads at ($3.0-$0.3)/M discount.
        let cache = stats["session"]["cache_savings_usd"].as_f64().unwrap();
        assert!((cache - 0.00054).abs() < 1e-9, "cache={cache}");
        // Input spend: 600*3e-6 + 200*3e-7 + 100*3.75e-6.
        let spend = stats["session"]["input_cost_usd"].as_f64().unwrap();
        assert!((spend - 0.002235).abs() < 1e-9, "spend={spend}");
        // Timeseries picked it up too.
        let day = ledger.timeseries_payload("day").unwrap();
        assert_eq!(day["points"][0]["requests"], 1);
        let hour = ledger.timeseries_payload("hour").unwrap();
        assert_eq!(hour["points"][0]["tokens_saved"], 400);
    }

    #[test]
    fn failed_requests_accrue_no_savings_anywhere() {
        let ledger = Ledger::in_memory();
        let mut ev = event(PRICED_MODEL);
        ev.failed = true;
        ledger.record(ev);
        let stats = ledger.stats_payload();
        assert_eq!(stats["requests"]["total"], 1);
        assert_eq!(stats["requests"]["failed"], 1);
        assert_eq!(stats["tokens"]["saved"], 0);
        assert_eq!(stats["session"]["input_cost_usd"], 0.0);
        assert_eq!(stats["lifetime"]["tokens_saved"], 0);
        let day = ledger.timeseries_payload("day").unwrap();
        assert_eq!(day["points"][0]["failed"], 1);
        assert_eq!(day["points"][0]["tokens_saved"], 0);
        // Still visible in the feed, flagged.
        let events = ledger.events_payload(10);
        assert_eq!(events["events"][0]["failed"], true);
        assert_eq!(events["events"][0]["tokens_saved"], 0);
    }

    #[test]
    fn headway_contract_keys_are_served() {
        let ledger = Ledger::in_memory();
        ledger.record(event(PRICED_MODEL));
        let stats = ledger.stats_payload();
        assert_eq!(stats["summary"]["api_requests"], 1);
        assert_eq!(stats["requests"]["failed"], 0);
        assert_eq!(stats["requests"]["cached"], 0);
        assert_eq!(stats["tokens"]["saved"], 400);
        assert_eq!(stats["tokens"]["proxy_compression_saved"], 400);
    }

    #[test]
    fn unknown_model_counts_tokens_but_zero_usd() {
        let ledger = Ledger::in_memory();
        ledger.record(event("model-not-in-any-price-table-xyz"));
        let stats = ledger.stats_payload();
        assert_eq!(stats["session"]["tokens_saved"], 400);
        assert_eq!(stats["session"]["savings_usd"], 0.0);
        assert_eq!(stats["session"]["input_cost_usd"], 0.0);
    }

    #[test]
    fn per_model_map_is_bounded_with_overflow_fold() {
        let ledger = Ledger::in_memory();
        for i in 0..(MAX_MODEL_ROWS + 25) {
            ledger.record(event(&format!("hostile-model-{i}")));
        }
        let stats = ledger.stats_payload();
        let models = stats["lifetime_by_model"].as_object().unwrap();
        assert!(models.len() <= MAX_MODEL_ROWS + 1);
        assert_eq!(models[OVERFLOW_KEY]["requests"], 25);
    }

    #[test]
    fn record_does_no_disk_io_until_flush() {
        let dir = tmpdir("io");
        let path = dir.join("stats.json");
        let ledger = Arc::new(Ledger::load(path.clone()));
        ledger.record(event(PRICED_MODEL));
        assert!(!path.exists(), "record() must not write");
        assert!(ledger.is_dirty());
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(ledger.flush());
        assert!(path.exists(), "flush() persists");
        assert!(!ledger.is_dirty());
        let leftovers: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains("tmp"))
            .collect();
        assert!(leftovers.is_empty(), "no temp litter");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn lifetime_survives_restart_and_session_resets() {
        let dir = tmpdir("restart");
        let path = dir.join("stats.json");
        let ledger = Arc::new(Ledger::load(path.clone()));
        ledger.record(event(PRICED_MODEL));
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(ledger.flush());
        drop(ledger);

        let reloaded = Ledger::load(path);
        let stats = reloaded.stats_payload();
        assert_eq!(stats["lifetime"]["requests"], 1);
        assert_eq!(stats["lifetime"]["tokens_saved"], 400);
        assert_eq!(stats["session"]["requests"], 0, "session is per-boot");
        // Day buckets survive → historical charts survive restarts.
        let day = reloaded.timeseries_payload("day").unwrap();
        assert_eq!(day["points"][0]["requests"], 1);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupt_file_is_preserved_aside_and_ledger_starts_fresh() {
        let dir = tmpdir("corrupt");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stats.json");
        std::fs::write(&path, b"{ not json !!").unwrap();
        let ledger = Ledger::load(path.clone());
        assert_eq!(ledger.stats_payload()["lifetime"]["requests"], 0);
        assert!(!path.exists(), "corrupt file moved aside");
        let preserved = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .any(|e| e.file_name().to_string_lossy().contains("corrupt"));
        assert!(preserved, "corrupt bytes preserved for inspection");
        assert_eq!(ledger.stats_payload()["persistence"]["healthy"], false);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn hand_edited_negative_usd_is_clamped_on_load() {
        let dir = tmpdir("clamp");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stats.json");
        std::fs::write(
            &path,
            br#"{"format":"headroom-native-stats","version":1,"lifetime":{"requests":1,"compression_savings_usd":-5.0}}"#,
        )
        .unwrap();
        let ledger = Ledger::load(path);
        assert_eq!(
            ledger.stats_payload()["lifetime"]["compression_savings_usd"],
            0.0
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn hour_and_day_buckets_are_bounded() {
        let dir = tmpdir("buckets");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stats.json");
        // Prebuild a file with too many bucket rows; load must trim
        // to the caps, keeping the newest.
        let mut days = serde_json::Map::new();
        for i in 0..500 {
            days.insert(
                format!("2025-{:02}-{:02}", (i / 28) % 12 + 1, i % 28 + 1),
                json!({"requests": 1}),
            );
        }
        std::fs::write(
            &path,
            serde_json::to_vec(&json!({
                "format": "headroom-native-stats",
                "version": 1,
                "days": days,
            }))
            .unwrap(),
        )
        .unwrap();
        let ledger = Ledger::load(path);
        let day = ledger.timeseries_payload("day").unwrap();
        assert!(day["points"].as_array().unwrap().len() <= MAX_DAY_ROWS);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn week_and_month_fold_from_days() {
        let ledger = Ledger::in_memory();
        {
            let mut inner = ledger.lock();
            // 2026-07-16 (Thu) and 2026-07-13 (Mon) share a week;
            // 2026-06-30 is a different week and month.
            for (day, reqs) in [("2026-07-16", 2), ("2026-07-13", 3), ("2026-06-30", 5)] {
                let t = inner.persisted.days.entry(day.into()).or_default();
                t.requests = reqs;
            }
        }
        let week = ledger.timeseries_payload("week").unwrap();
        let points = week["points"].as_array().unwrap();
        assert_eq!(points.len(), 2);
        assert_eq!(points[1]["timestamp"], "2026-07-13");
        assert_eq!(points[1]["requests"], 5, "Mon+Thu fold into one week");
        let month = ledger.timeseries_payload("month").unwrap();
        let points = month["points"].as_array().unwrap();
        assert_eq!(points[0]["timestamp"], "2026-06");
        assert_eq!(points[1]["timestamp"], "2026-07");
        assert!(ledger.timeseries_payload("fortnight").is_none());
    }

    #[test]
    fn recent_ring_is_bounded_and_newest_first() {
        let ledger = Ledger::in_memory();
        for i in 0..(RECENT_CAP + 10) {
            let mut ev = event(PRICED_MODEL);
            ev.request_id = format!("req-{i}");
            ledger.record(ev);
        }
        let events = ledger.events_payload(500);
        let list = events["events"].as_array().unwrap();
        assert_eq!(list.len(), RECENT_CAP);
        assert_eq!(list[0]["request_id"], format!("req-{}", RECENT_CAP + 9));
    }

    #[test]
    fn hostile_field_lengths_are_clamped() {
        let ledger = Ledger::in_memory();
        let long = "m".repeat(5000);
        let mut ev = RequestEvent::new(provider::BEDROCK, &long, &long);
        ev.tokens_before = 10;
        ev.tokens_after = 5;
        ledger.record(ev);
        let stats = ledger.stats_payload();
        let key = stats["lifetime_by_model"]
            .as_object()
            .unwrap()
            .keys()
            .next()
            .unwrap()
            .clone();
        assert!(key.len() <= MAX_FIELD_LEN);
        let events = ledger.events_payload(1);
        assert!(events["events"][0]["request_id"].as_str().unwrap().len() <= MAX_FIELD_LEN);
    }
}
