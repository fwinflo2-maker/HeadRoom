"""Cache-aware counterfactual for compression savings (realized input rate).

`_estimate_compression_savings_usd` priced every saved token at the model's
full list input rate, while `_estimate_input_cost_usd` right beside it prices
the same request's real spend on the cache read/write/uncached split.
Compression re-applies the same removals on every warm-prefix turn (clients
resend the original transcript), so on cache-heavy traffic most saved-token
instances would have billed at the provider's cache-read discount, not list —
flat list pricing overstates the layer roughly 7x on a 94%-read mix. Saved
tokens now price at the request's realized blended input rate whenever the
breakdown is available; behavior without a breakdown is unchanged.
"""

from __future__ import annotations

import types

import pytest

from headroom.proxy import savings_tracker as st
from headroom.proxy.savings_tracker import (
    _estimate_compression_savings_usd,
    estimate_request_savings_usd,
)

LIST = 10.0 / 1_000_000
CACHE_READ = 1.0 / 1_000_000
CACHE_WRITE = 12.5 / 1_000_000


def _fake_litellm() -> types.SimpleNamespace:
    # cost_per_token succeeding makes _resolve_litellm_model return the name as-is.
    return types.SimpleNamespace(
        model_cost={
            "m": {
                "input_cost_per_token": LIST,
                "cache_read_input_token_cost": CACHE_READ,
                "cache_creation_input_token_cost": CACHE_WRITE,
            }
        },
        cost_per_token=lambda **_kw: (0.0, 0.0),
    )


@pytest.fixture(autouse=True)
def fake_litellm(monkeypatch):
    monkeypatch.setattr(st, "_get_litellm_module", _fake_litellm)


def test_warm_mix_prices_at_realized_rate():
    # 94% cache reads / 4% writes / 2% uncached — the mix compression actually
    # re-applies removals into on long Claude Code sessions.
    read, write, uncached = 940_000, 40_000, 20_000
    saved = 100_000
    blended_rate = (read * CACHE_READ + write * CACHE_WRITE + uncached * LIST) / (
        read + write + uncached
    )
    got = _estimate_compression_savings_usd(
        "m",
        saved,
        cache_read_tokens=read,
        cache_write_tokens=write,
        uncached_input_tokens=uncached,
    )
    assert got == pytest.approx(saved * blended_rate)
    # The whole point: an order of magnitude below flat list on this mix.
    assert got < saved * LIST * 0.2


def test_cold_mix_prices_at_list():
    # A request with no cache traffic keeps first-ingest removals at list.
    saved = 100_000
    got = _estimate_compression_savings_usd("m", saved, uncached_input_tokens=500_000)
    assert got == pytest.approx(saved * LIST)


def test_no_breakdown_keeps_flat_list_pricing():
    # Callers without the split (legacy checkpoints) are byte-for-byte unchanged.
    assert _estimate_compression_savings_usd("m", 100_000) == pytest.approx(100_000 * LIST)


def test_estimate_request_savings_usd_threads_split():
    read, write, uncached = 900_000, 50_000, 50_000
    blended_rate = (read * CACHE_READ + write * CACHE_WRITE + uncached * LIST) / (
        read + write + uncached
    )
    priced = estimate_request_savings_usd(
        "m",
        compression_tokens_saved=10_000,
        cache_read_tokens=read,
        cache_write_tokens=write,
        uncached_input_tokens=uncached,
    )
    assert priced["compression"] == pytest.approx(10_000 * blended_rate)
    # Without the split the compression key keeps its historical list pricing.
    flat = estimate_request_savings_usd("m", compression_tokens_saved=10_000)
    assert flat["compression"] == pytest.approx(10_000 * LIST)


def test_record_request_fallback_uses_realized_rate(tmp_path):
    tracker = st.SavingsTracker(path=tmp_path / "savings.json")
    tracker.record_request(
        model="m",
        input_tokens=1_000_000,
        tokens_saved=100_000,
        cache_read_tokens=940_000,
        cache_write_tokens=40_000,
        uncached_input_tokens=20_000,
    )
    blended_rate = (940_000 * CACHE_READ + 40_000 * CACHE_WRITE + 20_000 * LIST) / 1_000_000
    got = tracker.snapshot()["lifetime"]["compression_savings_usd"]
    assert got == pytest.approx(100_000 * blended_rate, rel=1e-4)
