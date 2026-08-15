"""Regression tests for the stale-price-map bug (AQ-0609).

LiteLLM fills ``litellm.model_cost`` once at import. A proxy that stays up for
weeks prices everything against that snapshot, so a model released after the
process started is absent from the map. The lookup used to read "absent" as
"free" and return 0.0 forever, with no signal anywhere that it had done so.

Field evidence: a proxy started 2026-07-01 priced 25,400,013 ``claude-opus-5``
tokens at $0.00 across 3,962 consecutive checkpoints while pricing
``claude-sonnet-5`` at the normal $3/Mtok. Restarting the process fixed it.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from headroom.proxy import savings_tracker as st


class _FakeLiteLLM:
    """Minimal stand-in for the parts of litellm the tracker touches."""

    model_cost_map_url = "https://example.invalid/model_prices.json"

    def __init__(self, model_cost: dict[str, Any], refreshed: dict[str, Any] | None = None):
        self.model_cost = model_cost
        self._refreshed = refreshed or {}
        self.refresh_calls = 0

    def cost_per_token(self, **_kwargs: Any) -> tuple[float, float]:
        # Force _resolve_litellm_model down its exception path so it returns the
        # bare model name — that is the shape that actually reaches the lookup.
        raise RuntimeError("not supported")

    def get_model_cost_map(self, url: str) -> dict[str, Any]:
        assert url == self.model_cost_map_url
        self.refresh_calls += 1
        return self._refreshed


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate the module-level refresh/unpriced state between tests."""
    monkeypatch.setattr(st, "LITELLM_AVAILABLE", True)
    monkeypatch.setattr(st, "_price_map_last_refresh", 0.0)
    monkeypatch.setattr(st, "_price_map_refresh_in_flight", False)
    monkeypatch.setattr(st, "_unpriced_tokens", {})
    yield


def _run_refresh_synchronously(monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    """Replace the background thread with an inline call, keeping the test deterministic."""
    started: list[threading.Thread] = []
    real_thread = threading.Thread

    def _inline_thread(*args: Any, **kwargs: Any):
        target = kwargs.get("target")

        class _Inline:
            def start(self_inner) -> None:
                started.append(real_thread(target=target))
                if target is not None:
                    target()

        return _Inline()

    monkeypatch.setattr(st.threading, "Thread", _inline_thread)
    return started


def test_known_model_is_priced_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: a model in the map prices at its list rate. Unchanged behavior."""
    fake = _FakeLiteLLM({"claude-sonnet-5": {"input_cost_per_token": 3e-06}})
    monkeypatch.setattr(st, "litellm", fake)

    usd = st._estimate_compression_savings_usd("claude-sonnet-5", 1_000_000)

    assert usd == pytest.approx(3.0)
    assert st.unpriced_tokens_snapshot()["total"] == 0
    assert fake.refresh_calls == 0, "a priceable model must not trigger a refresh"


def test_unknown_model_is_counted_as_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core regression: a $0 must be *visible*, not silent.

    Before the fix this returned 0.0 and left no trace anywhere, which is why
    the dashboard read the same number for weeks.
    """
    fake = _FakeLiteLLM({"claude-sonnet-5": {"input_cost_per_token": 3e-06}})
    monkeypatch.setattr(st, "litellm", fake)
    _run_refresh_synchronously(monkeypatch)

    usd = st._estimate_compression_savings_usd("claude-opus-5", 25_400_013)

    assert usd == 0.0
    snapshot = st.unpriced_tokens_snapshot()
    assert snapshot["total"] == 25_400_013
    assert snapshot["by_model"] == {"claude-opus-5": 25_400_013}


def test_unknown_model_triggers_a_price_map_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miss must ask litellm for a fresh map rather than waiting for a restart."""
    fake = _FakeLiteLLM(
        {"claude-sonnet-5": {"input_cost_per_token": 3e-06}},
        refreshed={
            "claude-sonnet-5": {"input_cost_per_token": 2e-06},
            "claude-opus-5": {"input_cost_per_token": 5e-06},
        },
    )
    monkeypatch.setattr(st, "litellm", fake)
    _run_refresh_synchronously(monkeypatch)

    # First call misses and refreshes the map in place.
    first = st._estimate_compression_savings_usd("claude-opus-5", 1_000_000)
    assert first == 0.0
    assert fake.refresh_calls == 1

    # Second call now prices the model the running process had never heard of.
    # This is exactly what previously required killing and relaunching the proxy.
    second = st._estimate_compression_savings_usd("claude-opus-5", 1_000_000)
    assert second == pytest.approx(5.0)


def test_refresh_is_rate_limited_to_one_per_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that is genuinely unpriceable must not refetch on every request."""
    fake = _FakeLiteLLM({}, refreshed={})
    monkeypatch.setattr(st, "litellm", fake)
    _run_refresh_synchronously(monkeypatch)

    for _ in range(25):
        st._estimate_compression_savings_usd("some-unlisted-model", 1_000)

    assert fake.refresh_calls == 1, "TTL window must collapse repeated misses into one fetch"
    assert st.unpriced_tokens_snapshot()["total"] == 25_000, "every miss is still counted"


def test_refresh_failure_is_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pricing runs on the request-recording path; a network failure must not raise."""

    class _ExplodingLiteLLM(_FakeLiteLLM):
        def get_model_cost_map(self, url: str) -> dict[str, Any]:
            self.refresh_calls += 1
            raise ConnectionError("no network")

    fake = _ExplodingLiteLLM({})
    monkeypatch.setattr(st, "litellm", fake)
    _run_refresh_synchronously(monkeypatch)

    usd = st._estimate_compression_savings_usd("claude-opus-5", 500)

    assert usd == 0.0
    assert fake.refresh_calls == 1
    assert st.unpriced_tokens_snapshot()["total"] == 500
