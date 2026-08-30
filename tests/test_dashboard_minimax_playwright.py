"""Playwright coverage for MiniMax provider attribution in the dashboard."""

from __future__ import annotations

import copy

import pytest

from tests.test_dashboard_cache_lifetime_playwright import _open_dashboard
from tests.test_dashboard_cache_ttl_playwright import _sample_stats

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
sync_playwright = playwright.sync_playwright


def test_minimax_provider_chip_and_cost_render() -> None:
    stats = copy.deepcopy(_sample_stats())
    stats.setdefault("cost", {})["per_model"] = {
        "MiniMax-M3": {
            "requests": 7,
            "tokens_saved": 42_000,
            "tokens_sent": 18_000,
            "reduction_pct": 70.0,
            "input_cost_usd": 0.018,
        }
    }

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        _open_dashboard(page, stats)

        table = page.get_by_text("Per-Model Token Savings", exact=True).locator("../..")
        expect(table.get_by_text("minimax", exact=True)).to_be_visible()
        expect(table.get_by_text("MiniMax-M3", exact=True)).to_be_visible()
        expect(table.get_by_text("$0.018", exact=True)).to_be_visible()

        browser.close()
