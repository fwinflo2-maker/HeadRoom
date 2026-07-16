//! HTTP surface for the native savings ledger:
//! `GET /stats`, `GET /stats/timeseries`, `GET /stats/events`, and
//! the embedded `GET /dashboard` page.
//!
//! All handlers are read-only snapshots over
//! [`super::ledger::Ledger`] — no handler can mutate accounting
//! state. Mounted by [`crate::proxy::build_app`] when
//! `Config::stats` is on (default); when off, the paths fall through
//! to the catch-all forwarder like any other route.

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Response};
use axum::Json;
use serde::Deserialize;

use crate::proxy::AppState;

/// The dashboard page, embedded at compile time so the binary is
/// self-contained (no template dir to deploy, no CDN dependency).
const DASHBOARD_HTML: &str = include_str!("dashboard.html");

pub async fn handle_stats(State(state): State<AppState>) -> Response {
    Json(state.stats.stats_payload()).into_response()
}

#[derive(Deserialize)]
pub struct TimeseriesParams {
    /// hour | day | week | month (default: day)
    bucket: Option<String>,
}

pub async fn handle_timeseries(
    State(state): State<AppState>,
    Query(params): Query<TimeseriesParams>,
) -> Response {
    let bucket = params.bucket.as_deref().unwrap_or("day");
    match state.stats.timeseries_payload(bucket) {
        Some(payload) => Json(payload).into_response(),
        None => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": "unknown bucket",
                "allowed": ["hour", "day", "week", "month"],
            })),
        )
            .into_response(),
    }
}

#[derive(Deserialize)]
pub struct EventsParams {
    limit: Option<usize>,
}

pub async fn handle_events(
    State(state): State<AppState>,
    Query(params): Query<EventsParams>,
) -> Response {
    Json(state.stats.events_payload(params.limit.unwrap_or(50))).into_response()
}

/// `Html` already sets `text/html; charset=utf-8`.
pub async fn handle_dashboard() -> Response {
    Html(DASHBOARD_HTML).into_response()
}
