"""/stats must not publish output-shaping claims a deployment never performed.

The output-savings ledger persists across restarts, and its arms can hold
unshaped traffic from periods when the shaper was rollout-blocked or from
pre-fix conversation-key schemas. A stable install with the shaper
blocked_by_channel published a "measured" -147.9% output reduction straight
from that ledger — a claim about shaping it never did. The /stats seam now
gates the claim on the shaper being active; the ledger itself is untouched.
"""

from __future__ import annotations

from headroom.proxy.output_savings import SavingsEstimate, SavingsLedger, SavingsRecorder
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
    payload = _output_reduction_payload(True, empty)
    assert payload["available"] is False
    assert payload["active"] is True
    assert payload["method"] is None


def test_every_branch_emits_the_same_key_set():
    """A consumer reading ci_low_percent or band_is_ci must not KeyError on the
    inactive or no-data paths — the desktop app and dashboard both read these."""
    no_data = SavingsEstimate(
        tokens_saved=0.0,
        baseline_tokens=0.0,
        pct=0.0,
        ci_low_pct=0.0,
        ci_high_pct=0.0,
        n_requests=0,
        kind="estimated",
    )
    branches = [
        _output_reduction_payload(False, _poisoned_measured()),
        _output_reduction_payload(True, no_data),
        _output_reduction_payload(True, _poisoned_measured()),
        _output_reduction_payload(False),
    ]
    expected = set(branches[-1])
    for payload in branches:
        assert set(payload) == expected
        # Numeric totals stay numeric on every branch; only the band is nullable.
        assert isinstance(payload["tokens_saved"], int | float)
        assert isinstance(payload["baseline_tokens"], int | float)
        assert isinstance(payload["reduction_percent"], int | float)
        assert isinstance(payload["requests"], int)


def test_unshaped_treatment_request_is_not_recorded(tmp_path):
    """The gate above fixes the install that reported it; this fixes the next
    one. The stratum label is attached whenever the shaper is configured on,
    but shaping can still be a no-op — and a treatment arm holding unshaped
    output republishes the same bogus reduction the moment /stats unblocks."""
    recorder = SavingsRecorder(path=tmp_path / "ledger.json", flush_every=1)

    assert not recorder.record_from_labels(("output_shaper:stratum:opus|chat|m|tools",), 500)
    assert recorder._ledger.treatment == {}

    assert recorder.record_from_labels(
        ("output_shaper:stratum:opus|chat|m|tools", "output_shaper:verbosity:L2"), 500
    )
    assert recorder._ledger.treatment["opus|chat|m|tools"].n == 1

    # Control is unshaped by definition and keeps recording unconditionally.
    assert recorder.record_from_labels(("output_shaper:control:opus|chat|m|tools",), 900)
    assert recorder._ledger.control["opus|chat|m|tools"].n == 1


def test_ledger_written_before_the_rule_drops_its_arms_but_keeps_the_baseline():
    legacy = {
        "baseline": {"strata": {"opus|chat|m|tools": {"mean": 800.0, "var": 100.0, "n": 40}}},
        "treatment": {"opus|chat|m|tools": {"n": 19_644, "sum": 5_000_000.0, "sumsq": 1e12}},
        "control": {"opus|chat|m|tools": {"n": 12, "sum": 4_000.0, "sumsq": 1e6}},
    }
    ledger = SavingsLedger.from_dict(legacy)
    assert ledger.treatment == {}
    assert ledger.control == {}
    # The offline baseline costs a `learn --verbosity` run to rebuild and was
    # never the poisoned part.
    assert ledger.baseline.lookup("opus|chat|m|tools")[2] == 40
    # Round-tripping now carries the marker, so it is kept next time.
    assert SavingsLedger.from_dict(ledger.to_dict()).baseline.lookup("opus|chat|m|tools")[2] == 40
