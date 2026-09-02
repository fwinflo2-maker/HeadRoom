"""Tests for headroom.proxy.output_savings — the counterfactual estimator."""

from __future__ import annotations

import pytest

from headroom.proxy.output_savings import (
    LEDGER_SCHEMA_VERSION,
    BaselineModel,
    SavingsLedger,
    SavingsRecorder,
    assign_arm,
    conversation_key_from_body,
    echo_ratio,
    input_bucket,
    model_family,
    stratum_key,
    stratum_label,
)

# ---------------------------------------------------------------------------
# stratification primitives
# ---------------------------------------------------------------------------


class TestStratification:
    def test_input_buckets_monotone(self):
        assert input_bucket(0) == "xs"
        assert input_bucket(1_999) == "xs"
        assert input_bucket(2_000) == "s"
        assert input_bucket(8_000) == "m"
        assert input_bucket(32_000) == "l"
        assert input_bucket(200_000) == "xl"

    def test_model_family_collapses_point_releases(self):
        assert model_family("claude-opus-4-8") == "opus"
        assert model_family("claude-opus-4-7") == "opus"
        assert model_family("claude-sonnet-4-6") == "sonnet"
        assert model_family("gpt-4o") == "gpt"
        assert model_family("something-weird") == "other"

    def test_stratum_key_is_most_to_least_specific(self):
        key = stratum_key(
            turn_kind="new_user_ask", input_tokens=5000, model="claude-opus-4-8", has_tools=True
        )
        assert key == "opus|new_user_ask|s|tools"

    def test_stratum_key_distinguishes_tools(self):
        a = stratum_key(turn_kind="x", input_tokens=100, model="m", has_tools=True)
        b = stratum_key(turn_kind="x", input_tokens=100, model="m", has_tools=False)
        assert a != b


# ---------------------------------------------------------------------------
# holdout arm assignment
# ---------------------------------------------------------------------------


class TestArmAssignment:
    def test_zero_holdout_always_treatment(self):
        assert assign_arm("anything", 0.0) == "treatment"

    def test_full_holdout_always_control(self):
        assert assign_arm("anything", 1.0) == "control"

    def test_assignment_is_stable_for_same_key(self):
        assert assign_arm("conv-123", 0.5) == assign_arm("conv-123", 0.5)

    def test_roughly_matches_fraction(self):
        keys = [f"conv-{i}" for i in range(4000)]
        control = sum(1 for k in keys if assign_arm(k, 0.1) == "control")
        # 10% holdout over 4000 keys — allow generous slack for hash noise.
        assert 250 < control < 550

    def test_conversation_key_stable_across_turns(self):
        first = {
            "model": "claude-opus-4-8",
            "messages": [{"role": "user", "content": "build a cache"}],
        }
        later = {
            "model": "claude-opus-4-8",
            "messages": [
                {"role": "user", "content": "build a cache"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
            ],
        }
        assert conversation_key_from_body(first) == conversation_key_from_body(later)

    def test_conversation_key_differs_by_first_message(self):
        a = {"model": "m", "messages": [{"role": "user", "content": "task A"}]}
        b = {"model": "m", "messages": [{"role": "user", "content": "task B"}]}
        assert conversation_key_from_body(a) != conversation_key_from_body(b)

    def test_conversation_key_uses_responses_stable_metadata(self):
        a = {
            "model": "gpt-5",
            "client_metadata": {"session_id": "session-1"},
            "input": "task A",
        }
        b = {
            "model": "gpt-5",
            "client_metadata": {"session_id": "session-2"},
            "input": "task A",
        }
        assert conversation_key_from_body(a) != conversation_key_from_body(b)

    def test_conversation_key_does_not_use_responses_delta_text(self):
        user_turn = {
            "model": "gpt-5",
            "instructions": "same session instructions",
            "input": "task A",
        }
        tool_turn = {
            "model": "gpt-5",
            "instructions": "same session instructions",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "ok",
                }
            ],
        }
        assert conversation_key_from_body(user_turn) == conversation_key_from_body(tool_turn)

    def test_conversation_key_unwraps_ws_response_create(self):
        http_body = {"model": "gpt-5", "input": "build a cache"}
        ws_body = {
            "type": "response.create",
            "response": {"model": "gpt-5", "input": "build a cache"},
        }
        assert conversation_key_from_body(http_body) == conversation_key_from_body(ws_body)

    def test_conversation_key_uses_responses_conversation_id(self):
        a = {
            "model": "gpt-5",
            "conversation": "conv_1",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "task A"}],
                }
            ],
        }
        b = {
            "model": "gpt-5",
            "conversation": "conv_2",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "task B"}],
                }
            ],
        }
        assert conversation_key_from_body(a) != conversation_key_from_body(b)


# ---------------------------------------------------------------------------
# baseline model
# ---------------------------------------------------------------------------


class TestBaselineModel:
    def test_observe_and_lookup_exact(self):
        m = BaselineModel()
        for v in (100, 200, 300):
            m.observe("opus|new_user_ask|s|tools", v)
        mean, var, n = m.lookup("opus|new_user_ask|s|tools")
        assert mean == 200.0
        assert n == 3
        assert var > 0

    def test_lookup_backs_off_to_prefix(self):
        m = BaselineModel()
        m.observe("opus|new_user_ask|s|tools", 500)
        # Query a sibling stratum (different tools flag) — backs off on prefix.
        mean, _, n = m.lookup("opus|new_user_ask|s|notools")
        assert mean == 500.0
        assert n == 1

    def test_lookup_falls_back_to_global(self):
        m = BaselineModel()
        m.observe("opus|a|s|tools", 100)
        m.observe("sonnet|b|m|notools", 300)
        mean, _, n = m.lookup("gpt|totally|xl|tools")
        assert mean == 200.0  # global mean of 100 and 300
        assert n == 2

    def test_roundtrip_serialization(self):
        m = BaselineModel()
        for v in (10, 20, 30):
            m.observe("k|a|s|tools", v)
        m2 = BaselineModel.from_dict(m.to_dict())
        assert m2.lookup("k|a|s|tools") == m.lookup("k|a|s|tools")
        assert m2.total_samples == 3

    def test_merge_is_equivalent_to_observing_both_streams(self):
        # Merging two baselines must equal observing every value against one
        # model — same totals per stratum and same global fallback.
        a = BaselineModel()
        for v in (100, 200):
            a.observe("opus|new_user_ask|s|tools", v)
        b = BaselineModel()
        b.observe("opus|new_user_ask|s|tools", 300)
        b.observe("sonnet|unknown|m|notools", 50)

        a.merge(b)

        mean, _, n = a.lookup("opus|new_user_ask|s|tools")
        assert n == 3
        assert mean == 200.0  # (100 + 200 + 300) / 3
        assert a.total_samples == 4  # 3 + 1 across both strata

        reference = BaselineModel()
        for v in (100, 200, 300):
            reference.observe("opus|new_user_ask|s|tools", v)
        reference.observe("sonnet|unknown|m|notools", 50)
        assert a.to_dict() == reference.to_dict()


# ---------------------------------------------------------------------------
# synthetic-control estimate
# ---------------------------------------------------------------------------


class TestEstimateFromBaseline:
    def _ledger_with_baseline(self, baseline_val: float, n: int = 50) -> SavingsLedger:
        ledger = SavingsLedger()
        for _ in range(n):
            ledger.baseline.observe("opus|new_user_ask|s|tools", baseline_val)
        return ledger

    def test_positive_savings_when_treatment_below_baseline(self):
        ledger = self._ledger_with_baseline(1000.0)
        for _ in range(20):
            ledger.record("treatment", "opus|new_user_ask|s|tools", 700, shaper_active=True)
        est = ledger.estimate_from_baseline()
        assert est.kind == "estimated"
        assert est.n_requests == 20
        # 20 requests * (1000 - 700) = 6000 tokens saved.
        assert abs(est.tokens_saved - 6000) < 1e-6
        assert abs(est.pct - 30.0) < 1e-6

    def test_signed_delta_not_clamped(self):
        # A treatment request LARGER than baseline must reduce the total, not
        # be clamped to zero (clamping would bias the estimate upward).
        ledger = self._ledger_with_baseline(1000.0)
        ledger.record("treatment", "opus|new_user_ask|s|tools", 700, shaper_active=True)
        ledger.record("treatment", "opus|new_user_ask|s|tools", 1400, shaper_active=True)
        est = ledger.estimate_from_baseline()
        # (1000-700) + (1000-1400) = 300 - 400 = -100
        assert abs(est.tokens_saved - (-100)) < 1e-6

    def test_zero_baseline_samples_yields_zero(self):
        ledger = SavingsLedger()
        ledger.record("treatment", "opus|x|s|tools", 500, shaper_active=True)
        est = ledger.estimate_from_baseline()
        # No baseline at all -> global is empty -> nothing contributes.
        assert est.n_requests == 0
        assert est.tokens_saved == 0.0

    def test_ci_band_brackets_point_estimate(self):
        ledger = SavingsLedger()
        for v in (900, 1000, 1100):
            for _ in range(20):
                ledger.baseline.observe("opus|new_user_ask|s|tools", v)
        for v in (600, 700, 800):
            for _ in range(20):
                ledger.record("treatment", "opus|new_user_ask|s|tools", v, shaper_active=True)
        est = ledger.estimate_from_baseline()
        assert est.ci_low_pct <= est.pct <= est.ci_high_pct
        assert est.ci_low_pct < est.ci_high_pct  # nonzero band given spread


# ---------------------------------------------------------------------------
# A/B measured estimate
# ---------------------------------------------------------------------------


class TestEstimateFromHoldout:
    def test_none_without_control_data(self):
        ledger = SavingsLedger()
        ledger.record("treatment", "opus|x|s|tools", 500, shaper_active=True)
        assert ledger.estimate_from_holdout() is None

    def test_measured_difference_of_means(self):
        ledger = SavingsLedger()
        for _ in range(30):
            ledger.record("control", "opus|new_user_ask|s|tools", 1000, shaper_active=True)
            ledger.record("treatment", "opus|new_user_ask|s|tools", 750, shaper_active=True)
        est = ledger.estimate_from_holdout()
        assert est is not None
        assert est.kind == "measured"
        # 30 * (1000 - 750) = 7500 saved; 25% of the 1000 baseline.
        assert abs(est.tokens_saved - 7500) < 1e-6
        assert abs(est.pct - 25.0) < 1e-6

    def test_only_strata_present_in_both_arms_contribute(self):
        ledger = SavingsLedger()
        for _ in range(10):
            ledger.record("control", "opus|a|s|tools", 1000, shaper_active=True)
            ledger.record("treatment", "opus|a|s|tools", 800, shaper_active=True)
        # Treatment-only stratum must not contribute (no control to compare).
        ledger.record("treatment", "opus|b|m|notools", 50, shaper_active=True)
        est = ledger.estimate_from_holdout()
        assert est is not None
        assert est.n_requests == 10

    def test_best_estimate_prefers_measured(self):
        ledger = SavingsLedger()
        for _ in range(10):
            ledger.baseline.observe("opus|a|s|tools", 1000)
            ledger.record("control", "opus|a|s|tools", 1000, shaper_active=True)
            ledger.record("treatment", "opus|a|s|tools", 900, shaper_active=True)
        assert ledger.best_estimate().kind == "measured"

    def test_best_estimate_falls_back_to_estimated(self):
        ledger = SavingsLedger()
        for _ in range(10):
            ledger.baseline.observe("opus|a|s|tools", 1000)
            ledger.record("treatment", "opus|a|s|tools", 900, shaper_active=True)
        assert ledger.best_estimate().kind == "estimated"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


class TestLedgerPersistence:
    def test_roundtrip(self, tmp_path):
        ledger = SavingsLedger()
        ledger.baseline.observe("opus|a|s|tools", 1000)
        ledger.record("treatment", "opus|a|s|tools", 800, shaper_active=True)
        ledger.record("control", "opus|a|s|tools", 1000, shaper_active=True)
        path = tmp_path / "savings.json"
        ledger.save(path)
        loaded = SavingsLedger.load(path)
        assert loaded.estimate_from_baseline().tokens_saved == (
            ledger.estimate_from_baseline().tokens_saved
        )
        assert loaded.estimate_from_holdout() is not None

    def test_load_missing_returns_empty(self, tmp_path):
        ledger = SavingsLedger.load(tmp_path / "nope.json")
        assert ledger.baseline.total_samples == 0

    def test_load_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        ledger = SavingsLedger.load(p)
        assert ledger.baseline.total_samples == 0


# ---------------------------------------------------------------------------
# shaper-active tagging (#3395)
#
# The arms fill whenever the shaper is *configured* on, but shaping is inert at
# level 0 (cache mode forces it; a learned or env level selects it). Untagged
# observations are the same problem one upgrade earlier. Neither may reach an
# estimate: differencing two unshaped arms is how a rollout-blocked install
# published a "measured" -147.9% reduction over 19,644 requests.
# ---------------------------------------------------------------------------


class TestShaperActiveTagging:
    KEY = "opus|new_user_ask|s|tools"

    def _seeded(self) -> SavingsLedger:
        ledger = SavingsLedger()
        for _ in range(10):
            ledger.baseline.observe(self.KEY, 1000)
        return ledger

    def test_active_observations_are_counted(self):
        ledger = self._seeded()
        for _ in range(4):
            ledger.record("treatment", self.KEY, 700, shaper_active=True)

        est = ledger.estimate_from_baseline()
        assert est.n_requests == 4
        assert est.tokens_saved == pytest.approx(4 * 300)

    def test_inactive_observations_reach_no_estimate(self):
        ledger = self._seeded()
        for _ in range(4):
            ledger.record("treatment", self.KEY, 700, shaper_active=False)
            ledger.record("control", self.KEY, 1000, shaper_active=False)

        assert ledger.estimate_from_baseline().n_requests == 0
        assert ledger.estimate_from_holdout() is None
        assert ledger.best_estimate().n_requests == 0
        # Kept, but only as diagnostics — this is the number that explains a
        # small n to an operator instead of history appearing to vanish.
        assert ledger.inactive_requests == 8

    def test_inactive_arm_cannot_pair_with_an_active_one(self):
        # The era-mixing case: control accumulated while the shaper was inert,
        # treatment after it was switched on. Pairing them measures the traffic
        # drift between the two periods and calls it shaping.
        ledger = self._seeded()
        for _ in range(6):
            ledger.record("control", self.KEY, 2000, shaper_active=False)
        for _ in range(6):
            ledger.record("treatment", self.KEY, 800, shaper_active=True)

        assert ledger.estimate_from_holdout() is None
        # The synthetic-control tier still works off the shaped arm alone.
        assert ledger.estimate_from_baseline().n_requests == 6

    def test_measured_number_uses_active_arms_only(self):
        ledger = self._seeded()
        for _ in range(5):
            ledger.record("control", self.KEY, 1000, shaper_active=True)
            ledger.record("treatment", self.KEY, 750, shaper_active=True)
            # Inactive-period traffic in both arms, wildly off, must not move it.
            ledger.record("control", self.KEY, 10, shaper_active=False)
            ledger.record("treatment", self.KEY, 9000, shaper_active=False)

        est = ledger.estimate_from_holdout()
        assert est is not None
        assert est.kind == "measured"
        assert est.n_requests == 5
        assert est.pct == pytest.approx(25.0)

    def test_modelled_tier_ignores_inactive_traffic(self):
        # The weakest tier multiplies observed treatment output by a benchmark
        # factor; unshaped output there invents savings out of a constant.
        ledger = SavingsLedger()
        for _ in range(3):
            ledger.record("treatment", self.KEY, 1000, shaper_active=False)

        assert ledger.estimate_from_model(3) is None


class TestRecorderTagsFromTheLabel:
    KEY = "opus|new_user_ask|s|tools"

    def _recorder(self, tmp_path) -> SavingsRecorder:
        rec = SavingsRecorder(tmp_path / "savings.json", flush_every=1000)
        for _ in range(10):
            rec._ledger.baseline.observe(self.KEY, 1000)
        return rec

    def test_a_shaped_level_records_into_the_active_arm(self, tmp_path):
        rec = self._recorder(tmp_path)
        assert (
            rec.record_from_labels([stratum_label("treatment", self.KEY, verbosity_level=3)], 700)
            is True
        )

        assert rec._ledger.treatment[self.KEY].n == 1
        assert rec.estimate().n_requests == 1

    def test_level_zero_records_into_the_inactive_arm(self, tmp_path):
        # Level 0 is the documented "no steering" value — what cache mode
        # forces. The request happened; the shaping did not.
        rec = self._recorder(tmp_path)
        assert (
            rec.record_from_labels([stratum_label("treatment", self.KEY, verbosity_level=0)], 700)
            is True
        )

        assert self.KEY not in rec._ledger.treatment
        assert rec._ledger.inactive_treatment[self.KEY].n == 1
        assert rec.estimate().n_requests == 0

    def test_an_untagged_label_is_treated_as_inactive(self, tmp_path):
        # A label with no level carries no evidence that shaping ran, and
        # unprovable activity must never be able to inflate a claim.
        rec = self._recorder(tmp_path)
        assert rec.record_from_labels([stratum_label("treatment", self.KEY)], 700) is True

        assert self.KEY not in rec._ledger.treatment
        assert rec._ledger.inactive_treatment[self.KEY].n == 1
        assert rec.estimate().n_requests == 0

    def test_control_is_tagged_the_same_way(self, tmp_path):
        # Symmetry matters: filtering only the treatment arm would leave the
        # arms holding different populations and bias the difference of means.
        rec = self._recorder(tmp_path)
        rec.record_from_labels([stratum_label("control", self.KEY, verbosity_level=0)], 1000)
        rec.record_from_labels([stratum_label("control", self.KEY, verbosity_level=3)], 1000)

        assert rec._ledger.control[self.KEY].n == 1
        assert rec._ledger.inactive_control[self.KEY].n == 1


class TestLedgerSchemaMigration:
    KEY = "opus|new_user_ask|s|tools"

    def _legacy_file(self, tmp_path, *, with_arms: bool = True):
        """A ledger file in the pre-tag on-disk shape (no ``schema_version``)."""
        import json

        payload = {
            "baseline": {
                "strata": {self.KEY: {"n": 10, "sum": 10_000.0, "sumsq": 10_000_000.0}},
                "glob": {"n": 10, "sum": 10_000.0, "sumsq": 10_000_000.0},
            },
        }
        if with_arms:
            payload["treatment"] = {self.KEY: {"n": 19_644, "sum": 12_000_000.0, "sumsq": 1e13}}
            payload["control"] = {self.KEY: {"n": 500, "sum": 250_000.0, "sumsq": 1e11}}
        p = tmp_path / "savings.json"
        p.write_text(json.dumps(payload))
        return p

    def test_legacy_file_loads_without_error_and_drops_its_arms(self, tmp_path):
        ledger = SavingsLedger.load(self._legacy_file(tmp_path))

        assert ledger.treatment == {}
        assert ledger.control == {}
        assert ledger.estimate_from_holdout() is None
        assert ledger.best_estimate().n_requests == 0

    def test_legacy_baseline_survives_the_drop(self, tmp_path):
        # The baseline is learned offline from pre-shaper history — unshaped by
        # definition, never the poisoned part, and costly to rebuild.
        ledger = SavingsLedger.load(self._legacy_file(tmp_path))

        assert ledger.baseline.total_samples == 10
        assert ledger.baseline.lookup(self.KEY)[0] == 1000.0

    def test_dropping_legacy_arms_is_reported(self, tmp_path, caplog, monkeypatch):
        # Silently discarding accumulated history is exactly the failure mode
        # #18 fixed for corrupt files; say it out loud.
        monkeypatch.setattr("headroom.proxy.output_savings._legacy_arms_warned", False)
        with caplog.at_level("WARNING"):
            SavingsLedger.load(self._legacy_file(tmp_path))

        assert any("predates the shaper-active tag" in r.message for r in caplog.records)

    def test_a_baseline_only_legacy_file_is_not_warned_about(self, tmp_path, caplog, monkeypatch):
        # `learn --verbosity --apply` before any traffic writes exactly this.
        monkeypatch.setattr("headroom.proxy.output_savings._legacy_arms_warned", False)
        with caplog.at_level("WARNING"):
            ledger = SavingsLedger.load(self._legacy_file(tmp_path, with_arms=False))

        assert ledger.baseline.total_samples == 10
        assert caplog.records == []

    def test_upgraded_install_re_accumulates_from_live_traffic(self, tmp_path):
        # The upgrade contract: a smaller n, not an inherited claim.
        path = self._legacy_file(tmp_path)
        rec = SavingsRecorder(path, flush_every=1)
        rec.record_from_labels([stratum_label("treatment", self.KEY, verbosity_level=3)], 700)

        est = rec.estimate()
        assert est.n_requests == 1
        assert est.tokens_saved == pytest.approx(300.0)

    def test_current_schema_round_trips_both_arm_pairs(self, tmp_path):
        ledger = SavingsLedger()
        ledger.baseline.observe(self.KEY, 1000)
        ledger.record("treatment", self.KEY, 800, shaper_active=True)
        ledger.record("control", self.KEY, 1000, shaper_active=True)
        ledger.record("treatment", self.KEY, 4000, shaper_active=False)
        ledger.record("control", self.KEY, 4000, shaper_active=False)
        path = tmp_path / "savings.json"
        ledger.save(path)

        loaded = SavingsLedger.load(path)
        assert loaded.treatment[self.KEY].n == 1
        assert loaded.control[self.KEY].n == 1
        assert loaded.inactive_requests == 2
        assert loaded.estimate_from_holdout().pct == pytest.approx(20.0)

    def test_saved_file_declares_the_schema_version(self, tmp_path):
        import json

        path = tmp_path / "savings.json"
        SavingsLedger().save(path)

        assert json.loads(path.read_text())["schema_version"] == LEDGER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# echo ratio (direct waste signal)
# ---------------------------------------------------------------------------


class TestEchoRatio:
    def test_full_echo(self):
        ctx = "the quick brown fox jumps over the lazy dog every single time"
        assert echo_ratio(ctx, ctx, n=4) == 1.0

    def test_no_echo(self):
        out = "completely unrelated words appearing nowhere within the given source context here"
        ctx = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
        assert echo_ratio(out, ctx, n=4) == 0.0

    def test_partial_echo_between_zero_and_one(self):
        ctx = "alpha beta gamma delta epsilon zeta eta theta"
        out = "alpha beta gamma delta brand new tokens here now"
        r = echo_ratio(out, ctx, n=4)
        assert 0.0 < r < 1.0

    def test_short_output_returns_zero(self):
        assert echo_ratio("a b", "a b c d e f g h", n=8) == 0.0


# ---------------------------------------------------------------------------
# recorder baseline reload (learn-while-running)
# ---------------------------------------------------------------------------


class TestRecorderBaselineReload:
    """The recorder must pick up a baseline that ``learn --verbosity --apply``
    writes while the proxy is already running, and a flush must never overwrite
    that learned baseline with the recorder's own empty in-memory copy."""

    @staticmethod
    def _key() -> str:
        return SAMPLE_KEY

    def test_adopts_baseline_learned_after_start(self, tmp_path):
        path = str(tmp_path / "output_savings.json")
        key = self._key()

        recorder = SavingsRecorder(path, flush_every=1)
        for output_tokens in (200, 210, 190):
            recorder.record_from_labels(
                [stratum_label("treatment", key, verbosity_level=3)], output_tokens
            )

        # No baseline to compare against yet, so there is nothing to estimate.
        assert recorder.estimate().n_requests == 0

        # Simulate `learn --verbosity --apply` writing a baseline to the same
        # file while the recorder is live (no restart).
        learned = SavingsLedger.load(path)
        for output_tokens in (400, 420, 380, 410):
            learned.baseline.observe(key, output_tokens)
        learned.save(path)

        estimate = recorder.estimate()
        assert estimate.n_requests > 0
        assert estimate.kind == "estimated"
        assert estimate.tokens_saved > 0

    def test_flush_does_not_clobber_learned_baseline(self, tmp_path):
        path = str(tmp_path / "output_savings.json")
        key = self._key()

        # Recorder starts before any baseline exists, so its in-memory baseline
        # is empty.
        recorder = SavingsRecorder(path, flush_every=1)

        learned = SavingsLedger.load(path)
        for output_tokens in (400, 420, 380, 410):
            learned.baseline.observe(key, output_tokens)
        learned.save(path)
        assert SavingsLedger.load(path).baseline.total_samples == 4

        recorder.record_from_labels([stratum_label("treatment", key, verbosity_level=3)], 200)
        recorder.flush()

        # The flush must keep the learned baseline rather than writing the empty
        # in-memory one over it.
        assert SavingsLedger.load(path).baseline.total_samples == 4

    def test_does_not_downgrade_to_empty_disk_baseline(self, tmp_path):
        path = str(tmp_path / "output_savings.json")
        key = self._key()

        # Recorder already holds a learned baseline in memory.
        recorder = SavingsRecorder(path, flush_every=1)
        recorder._ledger.baseline.observe(key, 400)
        recorder._ledger.baseline.observe(key, 420)
        assert recorder._ledger.baseline.total_samples == 2

        # A stale/empty file on disk must not erase a baseline we already hold.
        SavingsLedger().save(path)
        recorder.flush()

        assert recorder._ledger.baseline.total_samples == 2

    def test_relearn_with_same_sample_count_is_adopted(self, tmp_path):
        path = str(tmp_path / "output_savings.json")
        key = self._key()

        recorder = SavingsRecorder(path, flush_every=1)
        for output_tokens in (200, 210, 190):
            recorder.record_from_labels(
                [stratum_label("treatment", key, verbosity_level=3)], output_tokens
            )

        # First learn writes a baseline; the recorder adopts it.
        first = SavingsLedger.load(path)
        for output_tokens in (400, 400, 400, 400):
            first.baseline.observe(key, output_tokens)
        first.save(path)
        baseline_tokens_v1 = recorder.estimate().baseline_tokens
        assert baseline_tokens_v1 > 0

        # Re-running learn replaces the baseline in place with the SAME number of
        # samples but different values. A sample-count guard would miss this; the
        # recorder must still pick the new baseline up.
        relearned = SavingsLedger.load(path)
        relearned.baseline = BaselineModel()
        for output_tokens in (800, 800, 800, 800):
            relearned.baseline.observe(key, output_tokens)
        relearned.save(path)

        assert recorder.estimate().baseline_tokens > baseline_tokens_v1


# ---------------------------------------------------------------------------
# flush durability + event-loop safety
# ---------------------------------------------------------------------------

# Deterministic stratum key shared by the recorder tests below.
SAMPLE_KEY = stratum_key(
    turn_kind="code",
    input_tokens=8000,
    model="claude-opus-4-8",
    has_tools=True,
)


class TestFlushDurability:
    def test_crash_mid_write_leaves_previous_ledger_intact(self, tmp_path, monkeypatch):
        import headroom.fsutil

        path = str(tmp_path / "output_savings.json")
        key = SAMPLE_KEY

        recorder = SavingsRecorder(path, flush_every=1)
        recorder.record_from_labels([stratum_label("treatment", key, verbosity_level=3)], 200)
        recorder.flush()
        assert SavingsLedger.load(path).treatment[key].n == 1

        def _die_before_rename(*args, **kwargs):
            raise OSError(5, "simulated crash before rename")

        monkeypatch.setattr(headroom.fsutil.os, "replace", _die_before_rename)
        recorder.record_from_labels([stratum_label("treatment", key, verbosity_level=3)], 210)
        recorder.flush()  # OSError swallowed by the recorder — fail-open by design

        # The pre-crash sample must survive and no temp residue may be left
        # behind: a failed save may not corrupt or clutter the ledger.
        assert SavingsLedger.load(path).treatment[key].n == 1
        assert not list(tmp_path.glob("*.tmp"))

    def test_corrupt_ledger_warns_and_starts_empty(self, tmp_path, caplog):
        import logging

        path = tmp_path / "output_savings.json"
        path.write_text("{not json")

        with caplog.at_level(logging.WARNING):
            SavingsRecorder(str(path))

        assert caplog.records, "corrupt ledger was swallowed silently"

    def test_emit_request_outcome_flushes_off_the_loop_thread(self, tmp_path, monkeypatch):
        import asyncio
        import threading

        from headroom.proxy.outcome import RequestOutcome, emit_request_outcome

        path = str(tmp_path / "output_savings.json")
        recorder = SavingsRecorder(path, flush_every=1)
        monkeypatch.setattr("headroom.proxy.output_savings.get_recorder", lambda: recorder)

        saved_on_threads = []
        real_save = SavingsLedger.save

        def _spy_save(self, save_path):
            saved_on_threads.append(threading.get_ident())
            real_save(self, save_path)

        monkeypatch.setattr(SavingsLedger, "save", _spy_save)

        class _Metrics:
            async def record_request(self, **kwargs):
                pass

        class _Handler:
            def __init__(self):
                self.metrics = _Metrics()
                self.cost_tracker = None
                self.logger = None

        outcome = RequestOutcome(
            request_id="req-shaper",
            provider="openai",
            model="gpt-5",
            status_code=200,
            original_tokens=100,
            optimized_tokens=80,
            output_tokens=50,
            tokens_saved=20,
            attempted_input_tokens=100,
            transforms_applied=(stratum_label("treatment", SAMPLE_KEY, verbosity_level=3),),
        )
        asyncio.run(emit_request_outcome(_Handler(), outcome))

        loop_thread = threading.get_ident()
        assert saved_on_threads, "flush never ran"
        assert all(t != loop_thread for t in saved_on_threads)


class TestModelledTier:
    """The fallback for a deployment with no counterfactual of its own.

    The factor table ships EMPTY: open-source Headroom applies steering but
    does not claim a savings figure it has not measured. Factors arrive either
    from a holdout (which outranks this tier entirely) or from an extension
    calling ``register_modelled_factors``. These tests therefore register their
    own factors and restore the table afterwards -- they exercise the
    arithmetic, which is permanent, not the numbers, which are not.
    """

    @staticmethod
    @pytest.fixture
    def factors():
        """Install factors for level 3, then restore the real table."""
        from headroom.proxy.output_savings import (
            MODELLED_REDUCTION,
            register_modelled_factors,
        )

        saved = dict(MODELLED_REDUCTION)
        register_modelled_factors(3, 0.20, 0.40)
        try:
            yield (0.20, 0.40)
        finally:
            MODELLED_REDUCTION.clear()
            MODELLED_REDUCTION.update(saved)

    @staticmethod
    def _ledger_with(observed_total: int, n: int):
        from headroom.proxy.output_savings import SavingsLedger, stratum_key

        ledger = SavingsLedger()
        key = stratum_key(
            turn_kind="new_user_ask", input_tokens=1000, model="claude-sonnet-5", has_tools=False
        )
        for _ in range(n):
            ledger.record("treatment", key, observed_total // n, shaper_active=True)
        return ledger

    def test_ships_empty_so_an_unmeasured_deployment_claims_nothing(self):
        """No factors by default -> no modelled estimate, at any level.

        The dash this produces is the point: it is the correct rendering of
        "not measured". A built-in constant would be a number nobody measured
        on this deployment's traffic, which is the failure mode the tiering
        exists to prevent.
        """
        from headroom.proxy.output_savings import MODELLED_REDUCTION

        assert MODELLED_REDUCTION == {}
        led = self._ledger_with(5_000, 5)
        assert all(led.estimate_from_model(lv) is None for lv in (1, 2, 3, 4))

    def test_registering_factors_enables_the_tier(self, factors):
        assert self._ledger_with(5_000, 5).estimate_from_model(3) is not None

    def test_nonsense_factors_are_rejected_at_registration(self):
        """r=0 and r=1 break the r/(1-r) inversion; catch it at the door."""
        from headroom.proxy.output_savings import register_modelled_factors

        for bad in ((0.0, 0.4), (1.0, 1.0), (-0.1, 0.4), (0.5, 1.2)):
            with pytest.raises(ValueError):
                register_modelled_factors(3, *bad)
        with pytest.raises(ValueError, match="exceeds optimistic"):
            register_modelled_factors(3, 0.5, 0.2)

    def test_saving_inverts_the_reduction_rather_than_scaling_by_it(self, factors):
        """Observed output is POST-shaping, so saved is observed*r/(1-r).

        The naive observed*r understates the saving. This is the single
        arithmetic mistake the tier can make, so it is pinned.

        r is read from the table rather than hardcoded: the factors are
        re-measured whenever the steering text changes, and a test that
        snapshots them fails on every remeasure while testing nothing about
        the arithmetic it exists to protect.
        """
        from headroom.proxy.output_savings import MODELLED_REDUCTION

        ledger = self._ledger_with(10_000, 10)
        est = ledger.estimate_from_model(3)
        assert est is not None
        r = MODELLED_REDUCTION[3][0]
        assert 0 < r < 1, "a reduction factor outside (0,1) makes the inversion nonsense"
        assert est.tokens_saved == pytest.approx(10_000 * r / (1 - r), rel=1e-6)
        assert est.tokens_saved > 10_000 * r, "naive scaling would understate"
        # baseline = what the unshaped run would have emitted
        assert est.baseline_tokens == pytest.approx(10_000 + est.tokens_saved, rel=1e-6)

    def test_kind_is_modelled_so_the_ui_can_refuse_to_call_it_a_ci(self, factors):
        est = self._ledger_with(5_000, 5).estimate_from_model(3)
        assert est is not None and est.kind == "modelled"

    def test_band_is_the_two_provider_spread(self, factors):
        from headroom.proxy.output_savings import MODELLED_REDUCTION

        low, high = MODELLED_REDUCTION[3]
        est = self._ledger_with(5_000, 5).estimate_from_model(3)
        assert est is not None
        assert est.ci_low_pct == pytest.approx(low * 100)
        assert est.ci_high_pct == pytest.approx(high * 100)
        assert low <= high, "conservative end must not exceed the optimistic one"
        assert est.pct == est.ci_low_pct, "headline uses the conservative end"

    def test_unbenchmarked_level_yields_nothing_rather_than_a_guess(self):
        assert self._ledger_with(5_000, 5).estimate_from_model(1) is None

    def test_no_traffic_yields_nothing(self):
        from headroom.proxy.output_savings import SavingsLedger

        assert SavingsLedger().estimate_from_model(3) is None

    def test_a_real_baseline_supersedes_the_model(self):
        """The modelled tier is last resort; a learned baseline outranks it."""
        from headroom.proxy.output_savings import BaselineModel, SavingsLedger, stratum_key

        key = stratum_key(
            turn_kind="new_user_ask", input_tokens=1000, model="claude-sonnet-5", has_tools=False
        )
        baseline = BaselineModel()
        for _ in range(50):
            baseline.observe(key, 2000)
        ledger = SavingsLedger(baseline=baseline)
        for _ in range(10):
            ledger.record("treatment", key, 1000, shaper_active=True)
        assert ledger.best_estimate(3).kind == "estimated"

    def test_without_a_level_behaviour_is_unchanged(self):
        """Existing callers that pass no level must not silently gain a number."""
        est = self._ledger_with(5_000, 5).best_estimate()
        assert est.kind == "estimated" and est.n_requests == 0
