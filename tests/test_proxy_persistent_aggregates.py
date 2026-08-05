from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.prometheus_metrics import PrometheusMetrics
from headroom.proxy.savings_tracker import SavingsTracker
from headroom.proxy.server import ProxyConfig, create_app


def _record_proxy_request(
    proxy,
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 120,
    output_tokens: int = 24,
    tokens_saved: int = 30,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    uncached_input_tokens: int = 0,
) -> None:
    if proxy.cost_tracker:
        proxy.cost_tracker.record_tokens(model, tokens_saved, input_tokens)
    asyncio.run(
        proxy.metrics.record_request(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_saved=tokens_saved,
            latency_ms=15.0,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            uncached_input_tokens=uncached_input_tokens,
        )
    )


def test_metric_scopes_survive_restart_and_current_process_resets(tmp_path, monkeypatch):
    savings_path = tmp_path / "proxy_savings.json"
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(cache_enabled=False, rate_limit_enabled=False, log_requests=False)

    with TestClient(create_app(config)) as client:
        proxy = client.app.state.proxy
        _record_proxy_request(
            proxy,
            cache_read_tokens=40,
            cache_write_tokens=60,
            cache_write_5m_tokens=10,
            cache_write_1h_tokens=50,
            uncached_input_tokens=20,
        )
        _record_proxy_request(
            proxy,
            cache_read_tokens=10,
            cache_write_tokens=90,
            cache_write_5m_tokens=15,
            cache_write_1h_tokens=75,
            uncached_input_tokens=30,
        )
        proxy.metrics.record_compression("smart_crusher", 100, 70)
        proxy.metrics.record_compression("smart_crusher", 80, 50)
        proxy.metrics.record_codex_ws_unit(
            strategy="smart_crusher",
            reason_category="tool_result",
            elapsed_ms=5.0,
            text_bytes=128,
            tokens_before=100,
            tokens_after=70,
            tokens_saved=30,
            modified=True,
            strategy_chain=["smart_crusher"],
            content_type="text",
            text_shape="json",
        )
        proxy.metrics.record_codex_ws_frame(
            elapsed_ms=9.0,
            bytes_before=256,
            bytes_after=180,
            attempted_tokens=100,
            tokens_saved=30,
            modified=True,
            strategy_chain=["smart_crusher"],
            final_strategies=["smart_crusher"],
        )
        asyncio.run(proxy.metrics.record_cache_bust(11))

        live_stats = client.get("/stats").json()
        live_scope = live_stats["metric_scopes"]
        assert live_scope["current_process"]["requests"]["total"] == 2
        assert live_scope["current_process"]["cache"]["requests"] == 2
        assert live_scope["current_process"]["cache"]["bust_count"] == 1
        assert live_scope["current_process"]["cache"]["cache_bust_count"] == 1
        assert live_scope["current_process"]["compression"]["tokens_saved"] == 60
        assert live_scope["current_process"]["compression"]["compressions_by_strategy"] == {
            "smart_crusher": 2
        }
        assert live_scope["current_process"]["codex_ws"]["units_total"] == 1
        assert live_scope["display_session"]["requests"]["total"] == 2
        assert live_scope["lifetime"]["requests"]["total"] == 2
        assert live_scope["lifetime"]["cache"]["cache_write_tokens"] == 150
        assert live_scope["lifetime"]["cache"]["cache_write_1h_tokens"] == 125
        assert live_scope["lifetime"]["compression"]["compressions_by_strategy"] == {
            "smart_crusher": 2
        }
        assert live_scope["lifetime"]["codex_ws"]["frames_attempted_total"] == 1
        assert live_scope["updated_at"] is not None
        assert live_scope["process_started_at"] is not None

    with TestClient(create_app(config)) as client:
        restarted = client.get("/stats").json()
        scope = restarted["metric_scopes"]
        assert scope["current_process"]["requests"]["total"] == 0
        assert scope["current_process"]["compression"]["tokens_saved"] == 0
        assert scope["current_process"]["codex_ws"]["units_total"] == 0
        assert scope["display_session"]["requests"]["total"] == 2
        assert scope["lifetime"]["requests"]["total"] == 2
        assert scope["lifetime"]["cache"]["requests"] == 2
        assert scope["lifetime"]["cache"]["hit_requests"] == 2
        assert scope["lifetime"]["cache"]["cache_read_tokens"] == 50
        assert scope["lifetime"]["cache"]["cache_write_tokens"] == 150
        assert scope["lifetime"]["cache"]["cache_write_5m_tokens"] == 25
        assert scope["lifetime"]["cache"]["cache_write_1h_tokens"] == 125
        assert scope["lifetime"]["cache"]["uncached_input_tokens"] == 50
        assert scope["lifetime"]["cache"]["bust_count"] == 1
        assert scope["lifetime"]["cache"]["cache_bust_count"] == 1
        assert scope["lifetime"]["cache"]["cache_bust_tokens_lost"] == 11
        assert scope["lifetime"]["compression"]["compressions_by_strategy"] == {"smart_crusher": 2}
        assert scope["lifetime"]["compression"]["tokens_saved_by_strategy"] == {"smart_crusher": 60}
        assert scope["lifetime"]["codex_ws"]["units_total"] == 1
        assert scope["lifetime"]["codex_ws"]["frames_attempted_total"] == 1

        metrics = client.get("/metrics").text
        assert "headroom_requests_total 0" in metrics
        assert "headroom_persistent_savings_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_read_tokens_total 50" in metrics
        assert "headroom_persistent_savings_cache_write_tokens_total 150" in metrics
        assert "headroom_persistent_savings_cache_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_hit_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_bust_total 1" in metrics
        assert "headroom_persistent_savings_cache_bust_tokens_lost_total 11" in metrics


def test_metric_scopes_migrate_v4_state_additively(tmp_path):
    path = tmp_path / "proxy_savings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "lifetime": {
                    "requests": 3,
                    "tokens_saved": 90,
                    "compression_savings_usd": 0.27,
                    "cache_read_tokens": 50,
                    "cache_savings_usd": 0.12,
                    "total_input_tokens": 360,
                    "total_input_cost_usd": 0.81,
                },
                "display_session": {
                    "requests": 1,
                    "tokens_saved": 30,
                    "compression_savings_usd": 0.09,
                    "cache_read_tokens": 10,
                    "cache_savings_usd": 0.03,
                    "total_input_tokens": 120,
                    "total_input_cost_usd": 0.27,
                    "started_at": "2026-07-14T12:00:00Z",
                    "last_activity_at": "2026-07-14T12:00:00Z",
                },
                "history": [],
                "projects": {},
                "by_model": {},
            }
        ),
        encoding="utf-8",
    )

    tracker = SavingsTracker(path=str(path))
    snapshot = tracker.snapshot()

    assert snapshot["schema_version"] == 5
    assert snapshot["lifetime"]["requests"] == 3
    lifetime = tracker.lifetime_response()
    display = tracker.display_session_response()
    assert lifetime["prefix_cache"]["cache_write_tokens"] == 0
    assert lifetime["compression"]["compressions_by_strategy"] == {}
    assert lifetime["codex_ws"]["units_total"] == 0
    assert display["prefix_cache"]["requests"] == 0


def test_metric_scopes_stateless_mode_writes_nothing_and_restarts_empty(tmp_path, monkeypatch):
    savings_path = tmp_path / "proxy_savings.json"
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(
        cache_enabled=False,
        rate_limit_enabled=False,
        log_requests=False,
        stateless=True,
    )

    with TestClient(create_app(config)) as client:
        proxy = client.app.state.proxy
        _record_proxy_request(proxy, tokens_saved=25, cache_read_tokens=15, cache_write_tokens=20)
        proxy.metrics.record_compression("smart_crusher", 100, 75)
        stats = client.get("/stats").json()
        assert stats["metric_scopes"]["current_process"]["requests"]["total"] == 1
        assert stats["metric_scopes"]["lifetime"]["requests"]["total"] == 1

    assert not savings_path.exists()

    with TestClient(create_app(config)) as client:
        stats = client.get("/stats").json()
        assert stats["metric_scopes"]["current_process"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["compression"]["compressions_by_strategy"] == {}


def test_metric_scopes_stateless_mode_ignores_existing_file(tmp_path, monkeypatch):
    savings_path = tmp_path / "proxy_savings.json"
    savings_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "updated_at": "2026-07-14T12:05:00Z",
                "lifetime": {
                    "requests": 1,
                    "tokens_saved": 3,
                    "compression_savings_usd": 0.01,
                    "cache_read_tokens": 0,
                    "cache_savings_usd": 0.0,
                    "total_input_tokens": 9,
                    "total_input_cost_usd": 0.02,
                    "aggregates": {
                        "cache": {},
                        "compression": {"compressions_by_strategy": {"diff": 1}},
                        "codex_ws": {},
                    },
                },
                "display_session": {
                    "requests": 1,
                    "tokens_saved": 3,
                    "compression_savings_usd": 0.01,
                    "cache_read_tokens": 0,
                    "cache_savings_usd": 0.0,
                    "total_input_tokens": 9,
                    "total_input_cost_usd": 0.02,
                    "savings_percent": 25.0,
                    "started_at": "2026-07-14T12:00:00Z",
                    "last_activity_at": "2026-07-14T12:05:00Z",
                    "aggregates": {
                        "cache": {},
                        "compression": {"compressions_by_strategy": {"diff": 1}},
                        "codex_ws": {},
                    },
                },
                "history": [],
                "projects": {},
                "by_model": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(
        cache_enabled=False,
        rate_limit_enabled=False,
        log_requests=False,
        stateless=True,
    )

    with TestClient(create_app(config)) as client:
        stats = client.get("/stats").json()
        assert stats["metric_scopes"]["current_process"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["compression"]["compressions_by_strategy"] == {}


def test_metric_scopes_cache_denominators_ignore_uncached_requests_consistently(
    tmp_path, monkeypatch
):
    savings_path = tmp_path / "proxy_savings.json"
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(cache_enabled=False, rate_limit_enabled=False, log_requests=False)

    with TestClient(create_app(config)) as client:
        proxy = client.app.state.proxy
        _record_proxy_request(
            proxy,
            input_tokens=1000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            uncached_input_tokens=1000,
        )
        _record_proxy_request(
            proxy,
            input_tokens=100,
            cache_read_tokens=100,
            cache_write_tokens=0,
            uncached_input_tokens=0,
        )
        stats = client.get("/stats").json()
        current_scope = stats["metric_scopes"]["current_process"]["cache"]
        lifetime_scope = stats["metric_scopes"]["lifetime"]["cache"]

        assert current_scope["uncached_input_tokens"] == 0
        assert lifetime_scope["uncached_input_tokens"] == 0
        assert current_scope["requests"] == 1
        assert lifetime_scope["requests"] == 1
        assert current_scope["hit_rate"] == 100.0
        assert lifetime_scope["hit_rate"] == 100.0


def test_aggregate_only_updates_survive_into_next_display_session_request(tmp_path):
    tracker = SavingsTracker(path=str(tmp_path / "proxy_savings.json"), save_flush_every=10)

    tracker.record_compression(
        strategy="smart_crusher",
        original_tokens=100,
        compressed_tokens=70,
        timestamp="2026-07-14T12:00:00Z",
    )
    assert tracker.display_session_response()["compression"]["compressions_by_strategy"] == {
        "smart_crusher": 1
    }

    tracker.record_lifetime_request(
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
        timestamp="2026-07-14T12:00:01Z",
    )
    assert tracker.display_session_response()["requests"]["total"] == 1
    assert tracker.display_session_response()["compression"]["compressions_by_strategy"] == {
        "smart_crusher": 1
    }


def test_zero_and_negative_savings_strategy_counts_persist(tmp_path):
    tracker = SavingsTracker(path=str(tmp_path / "proxy_savings.json"), save_flush_every=25)
    metrics = PrometheusMetrics(savings_tracker=tracker)

    metrics.record_compression("diff", 80, 80)
    metrics.record_compression("code_aware", 50, 70)
    tracker.flush()

    persisted = json.loads(Path(tracker.storage_path).read_text(encoding="utf-8"))
    compression = persisted["lifetime_metrics"]["compression"]
    assert compression["compressions_by_strategy"] == {"diff": 1, "code_aware": 1}
    assert compression["tokens_saved_by_strategy"] == {}


def test_aggregate_only_updates_stay_dirty_until_request_save(tmp_path):
    path = tmp_path / "proxy_savings.json"
    tracker = SavingsTracker(path=str(path), save_flush_every=6)

    tracker.record_compression(
        strategy="smart_crusher",
        original_tokens=100,
        compressed_tokens=70,
        timestamp="2026-07-14T12:00:00Z",
    )
    tracker.record_lifetime_cache_bust(tokens_lost=11)
    tracker.record_codex_ws_unit(
        strategy="smart_crusher",
        reason_category="tool_result",
        elapsed_ms=5.0,
        text_bytes=128,
        tokens_before=100,
        tokens_after=70,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        content_type="text",
        text_shape="json",
        timestamp="2026-07-14T12:00:02Z",
    )
    tracker.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
        timestamp="2026-07-14T12:00:03Z",
    )

    assert not path.exists()

    tracker.record_lifetime_request(
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
        timestamp="2026-07-14T12:00:04Z",
    )
    assert not path.exists()

    tracker.record_lifetime_request(
        model="gpt-4o",
        input_tokens=80,
        tokens_saved=10,
        timestamp="2026-07-14T12:00:05Z",
    )

    persisted = json.loads(path.read_text())
    lifetime = persisted["lifetime_metrics"]
    cache = lifetime["prefix_cache"]
    compression = lifetime["compression"]
    codex_ws = lifetime["codex_ws"]
    assert compression["compressions_by_strategy"] == {"smart_crusher": 1}
    assert compression["tokens_saved_by_strategy"] == {"smart_crusher": 30}
    assert cache["bust_count"] == 1
    assert cache["bust_tokens"] == 11
    assert codex_ws["units_total"] == 1
    assert codex_ws["frames_attempted_total"] == 1


def test_graceful_flush_persists_aggregate_only_updates(tmp_path):
    path = tmp_path / "proxy_savings.json"
    metrics = PrometheusMetrics(savings_tracker=SavingsTracker(path=str(path), save_flush_every=25))

    metrics.record_compression("smart_crusher", 100, 70)
    asyncio.run(metrics.record_cache_bust(11))
    metrics.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
    )
    assert not path.exists()

    metrics.savings_tracker.flush()
    persisted = json.loads(path.read_text())
    assert persisted["lifetime_metrics"]["compression"]["compressions_by_strategy"] == {
        "smart_crusher": 1
    }
    assert persisted["lifetime_metrics"]["prefix_cache"]["bust_count"] == 1
    assert persisted["lifetime_metrics"]["codex_ws"]["frames_attempted_total"] == 1


FIXED_NOW = datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)


def _expired_session_seed(
    *, request_total: int = 3, session_last_activity: datetime = FIXED_NOW
) -> dict:
    """State whose display session last saw activity at session_last_activity.

    ``session_last_activity`` defaults to ``FIXED_NOW`` so the session is still
    fresh inside the inactivity window at construction; the caller advances the
    clock past ``display_session_inactivity_minutes`` to cross the boundary.
    """
    return {
        "schema_version": 5,
        "lifetime": {
            "requests": 0,
            "tokens_saved": 0,
            "compression_savings_usd": 0.0,
            "cache_read_tokens": 0,
            "cache_savings_usd": 0.0,
            "total_input_tokens": 0,
            "total_input_cost_usd": 0.0,
        },
        "display_session": {
            "requests": 0,
            "tokens_saved": 0,
            "compression_savings_usd": 0.0,
            "cache_read_tokens": 0,
            "cache_savings_usd": 0.0,
            "total_input_tokens": 0,
            "total_input_cost_usd": 0.0,
            "started_at": "2026-01-01T00:00:00Z",
            "last_activity_at": "2026-01-01T00:00:00Z",
        },
        "history": [],
        "projects": {},
        "by_model": {},
        "lifetime_metrics": {
            "started_at": "2026-01-01T00:00:00Z",
            "last_activity_at": "2026-01-01T00:00:00Z",
            "full_fidelity_started_at": "2026-01-01T00:00:00Z",
            "requests": {
                "total": request_total,
                "cached": 1,
                "failed": 1,
                "rate_limited": 1,
                "by_provider": {"openai": request_total},
                "by_stack": {"cli": request_total},
            },
            "tokens": {
                "input": 900,
                "output": 60,
                "attempted_input": 960,
                "saved": 90,
            },
            "prefix_cache": {
                "requests": 1,
                "hit_requests": 1,
                "cache_read_tokens": 40,
                "cache_write_tokens": 60,
                "cache_write_5m_tokens": 10,
                "cache_write_1h_tokens": 50,
                "uncached_input_tokens": 20,
                "cache_write_5m_requests": 1,
                "cache_write_1h_requests": 1,
                "bust_count": 1,
                "bust_tokens": 11,
                "misses_by_reason": {"prefix_change": 1},
                "by_provider": {"openai": 1},
            },
            "cost": {
                "input_usd": 1.0,
                "compression_savings_usd": 0.3,
                "cache_savings_usd": 0.1,
            },
            "compression": {
                "compressions_by_strategy": {"smart_crusher": 5},
                "tokens_saved_by_strategy": {"smart_crusher": 150},
            },
            "codex_ws": {
                "units_total": 2,
                "units_modified_total": 1,
                "units_to_kompress_total": 0,
                "units_kompress_attempted_total": 0,
                "units_by_strategy": {"smart_crusher": 2},
                "units_by_category": {"tool_result": 2},
                "units_by_content_type": {"text": 2},
                "units_by_text_shape": {"json": 2},
                "unit_elapsed_ms_sum": 10.0,
                "unit_elapsed_ms_max": 5.0,
                "unit_bytes_sum": 256,
                "unit_tokens_before_sum": 200,
                "unit_tokens_after_sum": 140,
                "unit_tokens_saved_sum": 60,
                "frames_attempted_total": 1,
                "frames_compressed_total": 1,
                "frames_failed_total": 0,
                "frames_to_kompress_total": 0,
                "frames_kompress_attempted_total": 0,
                "frame_elapsed_ms_sum": 9.0,
                "frame_elapsed_ms_max": 9.0,
                "frame_bytes_before_sum": 256,
                "frame_bytes_after_sum": 180,
                "frame_attempted_tokens_sum": 100,
                "frame_tokens_saved_sum": 30,
            },
            "waste_signals": {"repetition": 7},
            "models": {
                "tracked": {},
                "other": {
                    "requests": request_total,
                    "input_tokens": 900,
                    "output_tokens": 60,
                    "attempted_input_tokens": 960,
                    "tokens_saved": 90,
                    "last_activity_at": "2026-01-01T00:00:00Z",
                },
            },
            "persistence": {"last_saved_at": "2026-01-01T00:00:00Z"},
        },
        "display_session_metrics": {
            "started_at": session_last_activity.isoformat(),
            "last_activity_at": session_last_activity.isoformat(),
            "full_fidelity_started_at": session_last_activity.isoformat(),
            "requests": {
                "total": request_total,
                "cached": 1,
                "failed": 1,
                "rate_limited": 1,
                "by_provider": {"openai": request_total},
                "by_stack": {"cli": request_total},
            },
            "tokens": {
                "input": 900,
                "output": 60,
                "attempted_input": 960,
                "saved": 90,
            },
            "prefix_cache": {
                "requests": 1,
                "hit_requests": 1,
                "cache_read_tokens": 40,
                "cache_write_tokens": 60,
                "cache_write_5m_tokens": 10,
                "cache_write_1h_tokens": 50,
                "uncached_input_tokens": 20,
                "cache_write_5m_requests": 1,
                "cache_write_1h_requests": 1,
                "bust_count": 1,
                "bust_tokens": 11,
                "misses_by_reason": {"prefix_change": 1},
                "by_provider": {"openai": 1},
            },
            "cost": {
                "input_usd": 1.0,
                "compression_savings_usd": 0.3,
                "cache_savings_usd": 0.1,
            },
            "compression": {
                "compressions_by_strategy": {"smart_crusher": 5},
                "tokens_saved_by_strategy": {"smart_crusher": 150},
            },
            "codex_ws": {
                "units_total": 2,
                "units_modified_total": 1,
                "units_to_kompress_total": 0,
                "units_kompress_attempted_total": 0,
                "units_by_strategy": {"smart_crusher": 2},
                "units_by_category": {"tool_result": 2},
                "units_by_content_type": {"text": 2},
                "units_by_text_shape": {"json": 2},
                "unit_elapsed_ms_sum": 10.0,
                "unit_elapsed_ms_max": 5.0,
                "unit_bytes_sum": 256,
                "unit_tokens_before_sum": 200,
                "unit_tokens_after_sum": 140,
                "unit_tokens_saved_sum": 60,
                "frames_attempted_total": 1,
                "frames_compressed_total": 1,
                "frames_failed_total": 0,
                "frames_to_kompress_total": 0,
                "frames_kompress_attempted_total": 0,
                "frame_elapsed_ms_sum": 9.0,
                "frame_elapsed_ms_max": 9.0,
                "frame_bytes_before_sum": 256,
                "frame_bytes_after_sum": 180,
                "frame_attempted_tokens_sum": 100,
                "frame_tokens_saved_sum": 30,
            },
            "waste_signals": {"repetition": 7},
            "models": {
                "tracked": {},
                "other": {
                    "requests": request_total,
                    "input_tokens": 900,
                    "output_tokens": 60,
                    "attempted_input_tokens": 960,
                    "tokens_saved": 90,
                    "last_activity_at": "2026-01-01T00:00:00Z",
                },
            },
            "persistence": {"last_saved_at": "2026-01-01T00:00:00Z"},
        },
    }


class _FakeClock:
    def __init__(self) -> None:
        self.now = FIXED_NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now = self.now + timedelta(minutes=minutes)


def _expired_tracker(tmp_path, clock: _FakeClock) -> SavingsTracker:
    path = tmp_path / "proxy_savings.json"
    path.write_text(json.dumps(_expired_session_seed()), encoding="utf-8")
    tracker = SavingsTracker(
        path=str(path),
        now=clock,
    )
    clock.advance(minutes=180)
    return tracker


def test_expired_display_session_resets_when_compression_precedes_request(tmp_path):
    clock = _FakeClock()
    tracker = _expired_tracker(tmp_path, clock)

    tracker.record_compression(strategy="smart_crusher", original_tokens=100, compressed_tokens=70)
    tracker.record_lifetime_request(
        provider="anthropic",
        stack="codex",
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
    )

    display = tracker.display_session_response()
    assert display["requests"]["total"] == 1
    assert display["requests"]["by_provider"] == {"anthropic": 1}
    assert display["requests"]["by_stack"] == {"codex": 1}
    assert display["tokens"]["input"] == 120
    assert display["tokens"]["saved"] == 30
    assert display["tokens"]["output"] == 0
    assert display["compression"]["compressions_by_strategy"] == {"smart_crusher": 1}
    assert display["compression"]["tokens_saved_by_strategy"] == {"smart_crusher": 30}
    assert "openai" not in display["requests"]["by_provider"]
    assert "cli" not in display["requests"]["by_stack"]


def test_expired_display_session_resets_when_codex_ws_precedes_request(tmp_path):
    clock = _FakeClock()
    tracker = _expired_tracker(tmp_path, clock)

    tracker.record_codex_ws_unit(
        strategy="smart_crusher",
        reason_category="tool_result",
        elapsed_ms=5.0,
        text_bytes=128,
        tokens_before=100,
        tokens_after=70,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        content_type="text",
        text_shape="json",
    )
    tracker.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
    )
    tracker.record_lifetime_request(
        provider="anthropic",
        stack="codex",
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
    )

    display = tracker.display_session_response()
    assert display["requests"]["total"] == 1
    assert display["requests"]["by_provider"] == {"anthropic": 1}
    assert display["tokens"]["saved"] == 30
    assert display["codex_ws"]["units_total"] == 1
    assert display["codex_ws"]["units_modified_total"] == 1
    assert display["codex_ws"]["unit_tokens_saved_sum"] == 30
    assert display["codex_ws"]["frames_attempted_total"] == 1
    assert display["codex_ws"]["frame_tokens_saved_sum"] == 30


def test_aggregate_only_writers_reset_expired_display_session(tmp_path):
    clock = _FakeClock()

    tracker = _expired_tracker(tmp_path, clock)
    tracker.record_lifetime_stack("codex")
    display = tracker.display_session_response()
    assert display["requests"]["total"] == 0
    assert display["requests"]["by_stack"] == {"codex": 1}
    assert "cli" not in display["requests"]["by_stack"]

    tracker = _expired_tracker(tmp_path, clock)
    tracker.record_lifetime_failed(provider="anthropic", model="gpt-4o")
    display = tracker.display_session_response()
    assert display["requests"]["failed"] == 1
    assert display["requests"]["total"] == 0

    tracker = _expired_tracker(tmp_path, clock)
    tracker.record_lifetime_rate_limited(provider="anthropic", model="gpt-4o")
    display = tracker.display_session_response()
    assert display["requests"]["rate_limited"] == 1
    assert display["requests"]["total"] == 0

    tracker = _expired_tracker(tmp_path, clock)
    tracker.record_lifetime_cache_bust(tokens_lost=11)
    display = tracker.display_session_response()
    assert display["prefix_cache"]["bust_count"] == 1
    assert display["prefix_cache"]["bust_tokens"] == 11

    tracker = _expired_tracker(tmp_path, clock)
    tracker.record_lifetime_cache_miss(provider="anthropic", reason="prefix_change")
    display = tracker.display_session_response()
    assert display["prefix_cache"]["misses_by_reason"] == {"prefix_change": 1}


def test_read_path_reset_stays_in_memory_until_next_record_save(tmp_path):
    path = tmp_path / "proxy_savings.json"
    path.write_text(json.dumps(_expired_session_seed()), encoding="utf-8")
    clock = _FakeClock()
    tracker = SavingsTracker(path=str(path), now=clock)
    clock.advance(minutes=180)

    display = tracker.display_session_response()
    assert display["requests"]["total"] == 0

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["display_session_metrics"]["requests"]["total"] == 3

    tracker.record_compression(strategy="smart_crusher", original_tokens=100, compressed_tokens=70)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["display_session_metrics"]["requests"]["total"] == 0
    assert persisted["display_session_metrics"]["compression"]["compressions_by_strategy"] == {
        "smart_crusher": 1
    }


_AGGREGATE_RECORDERS = {
    "stack": lambda tracker: tracker.record_lifetime_stack("codex"),
    "failed": lambda tracker: tracker.record_lifetime_failed(provider="anthropic", model="gpt-4o"),
    "rate_limited": lambda tracker: tracker.record_lifetime_rate_limited(
        provider="anthropic", model="gpt-4o"
    ),
    "cache_bust": lambda tracker: tracker.record_lifetime_cache_bust(tokens_lost=5),
    "cache_miss": lambda tracker: tracker.record_lifetime_cache_miss(
        provider="anthropic", reason="prefix_change"
    ),
    "compression": lambda tracker: tracker.record_compression(
        strategy="smart_crusher", original_tokens=100, compressed_tokens=70
    ),
    "codex_ws_unit": lambda tracker: tracker.record_codex_ws_unit(
        strategy="smart_crusher",
        reason_category="tool_result",
        elapsed_ms=5.0,
        text_bytes=128,
        tokens_before=100,
        tokens_after=70,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        content_type="text",
        text_shape="json",
    ),
    "codex_ws_frame": lambda tracker: tracker.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
    ),
    "request": lambda tracker: tracker.record_lifetime_request(
        provider="anthropic",
        stack="codex",
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
    ),
}


def test_expired_prior_session_totals_gone_from_display_but_kept_in_lifetime(tmp_path):
    for method in _AGGREGATE_RECORDERS:
        clock = _FakeClock()
        tracker = _expired_tracker(tmp_path, clock)
        _AGGREGATE_RECORDERS[method](tracker)

        display = tracker.display_session_response()
        assert "openai" not in display["requests"]["by_provider"]
        assert "cli" not in display["requests"]["by_stack"]
        assert display["tokens"]["saved"] < 90
        assert display["prefix_cache"]["bust_tokens"] < 11

        lifetime = tracker._persistent_metrics.to_dict()
        assert "openai" in lifetime["requests"]["by_provider"]
        assert "cli" in lifetime["requests"]["by_stack"]
        assert lifetime["tokens"]["saved"] >= 90
        assert lifetime["prefix_cache"]["bust_tokens"] >= 11
