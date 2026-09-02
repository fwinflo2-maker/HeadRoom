"""/stats must not publish output-shaping claims a deployment never performed.

The output-savings ledger persists across restarts, and its arms can hold
unshaped traffic from periods when the shaper was rollout-blocked or from
pre-fix conversation-key schemas. A stable install with the shaper
blocked_by_channel published a "measured" -147.9% output reduction straight
from that ledger — a claim about shaping it never did. The /stats seam now
gates the claim on the shaper being active; the ledger itself is untouched.
"""

from __future__ import annotations

from headroom.proxy.output_savings import SavingsEstimate
from headroom.proxy.server import _output_reduction_payload


def _poisoned_measured() -> SavingsEstimate:
    # The audited real-world shape: blocked stable channel, lopsided legacy
    # arms, "measured" -147.9% over 19,644 requests.
    return SavingsEstimate(
        tokens_saved=-7_522_490,
        baseline_tokens=5_086_874,
        pct=-147.9,
        ci_low_pct=-160.2,
        ci_high_pct=-135.6,
        n_requests=19_644,
        kind="measured",
    )


def test_inactive_shaper_claims_zero_regardless_of_ledger():
    payload = _output_reduction_payload(False, _poisoned_measured())
    assert payload["active"] is False
    assert payload["method"] == "inactive"
    assert payload["tokens_saved"] == 0
    assert payload["reduction_percent"] == 0.0
    assert payload["requests"] == 0
    # Still an available, well-formed layer entry, so consumers can render
    # "inactive" instead of mistaking absence for zero measurement.
    assert payload["available"] is True


def test_active_shaper_publishes_the_estimate_as_before():
    payload = _output_reduction_payload(True, _poisoned_measured())
    assert payload["active"] is True
    assert payload["available"] is True
    assert payload["method"] == "measured"
    assert payload["band_is_ci"] is True
    assert payload["tokens_saved"] == -7_522_490
    assert payload["reduction_percent"] == -147.9
    assert payload["requests"] == 19_644


def test_active_shaper_without_data_stays_unavailable():
    empty = SavingsEstimate(
        tokens_saved=0.0,
        baseline_tokens=0.0,
        pct=0.0,
        ci_low_pct=0.0,
        ci_high_pct=0.0,
        n_requests=0,
        kind="estimated",
    )
    assert _output_reduction_payload(True, empty) == {
        "available": False,
        "active": True,
    }
