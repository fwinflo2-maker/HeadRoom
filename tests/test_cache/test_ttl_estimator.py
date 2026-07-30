"""Tests for the offline cache-TTL estimator (`headroom-cache-ttl`)."""

from __future__ import annotations

import json

import pytest

from headroom.cache.ttl_estimator import (
    estimate_ttls,
    main,
    write_learned,
)
from headroom.cache.ttl_observations import resolve_learned_ttl


def _row(
    provider: str = "openai",
    model: str = "gpt-5.5",
    *,
    idle: float,
    hit: bool,
    reason: str | None = None,
) -> dict:
    return {
        "ts": 1000.0,
        "provider": provider,
        "model": model,
        "reason": reason if reason is not None else ("hit" if hit else "ttl_expiry"),
        "idle_seconds": idle,
        "ttl_assumed": 300,
        "is_miss": not hit,
        "cache_read": 1000 if hit else 0,
        "expected_cached": 1000,
    }


def _corpus(hit_idles: list[float], expiry_idles: list[float]) -> list[dict]:
    return [_row(idle=i, hit=True) for i in hit_idles] + [
        _row(idle=i, hit=False) for i in expiry_idles
    ]


class TestEstimateTtls:
    def test_upper_bound_of_interval_is_emitted(self):
        # Alive at up to 480s, first observed death beyond that at 600s
        # -> TTL estimate is the safe upper end: 600.
        table = estimate_ttls(_corpus([120, 300, 480], [600, 900, 1200]))
        assert table["openai"]["ttl_seconds"] == 600
        assert table["openai/gpt-5.5"]["ttl_seconds"] == 600
        assert table["openai"]["max_hit_idle"] == 480

    def test_death_at_or_below_max_hit_idle_is_not_an_upper_bound(self):
        # Deaths at 400/450 sit below a hit at 480 (variable eviction);
        # only the 700s death is beyond every observed life.
        table = estimate_ttls(_corpus([120, 300, 480], [400, 450, 700]))
        assert table["openai"]["ttl_seconds"] == 700

    def test_no_death_beyond_life_skips_key(self):
        # Every observed death overlaps the life range: no safe upper end.
        table = estimate_ttls(_corpus([120, 480, 900], [400, 450, 600]))
        assert table == {}

    def test_insufficient_samples_skip_key(self):
        assert estimate_ttls(_corpus([480], [600, 700, 800])) == {}  # 1 hit < 3
        assert estimate_ttls(_corpus([100, 200, 480], [600])) == {}  # 1 expiry < 3
        # Thresholds are tunable.
        table = estimate_ttls(_corpus([480], [600]), min_hits=1, min_expiry_misses=1)
        assert table["openai"]["ttl_seconds"] == 600

    def test_non_ttl_expiry_misses_are_ignored(self):
        rows = _corpus([100, 200, 480], [600, 700]) + [
            _row(idle=550, hit=False, reason="prefix_change"),
            _row(idle=560, hit=False, reason="cold_start"),
        ]
        # prefix_change/cold_start misses say nothing about TTL: still only
        # 2 ttl_expiry rows -> below the min_expiry_misses=3 floor.
        assert estimate_ttls(rows) == {}

    def test_zero_idle_and_malformed_rows_are_ignored(self):
        rows = _corpus([100, 200, 480], [600, 700, 800]) + [
            _row(idle=0.0, hit=True),
            {"provider": "", "idle_seconds": 50, "is_miss": False},
            {"provider": "openai", "idle_seconds": "nan-ish"},
            "not a dict",  # type: ignore[list-item]
        ]
        assert estimate_ttls(rows)["openai"]["ttl_seconds"] == 600

    def test_provider_aggregate_spans_models(self):
        rows = _corpus([100, 480, 200], [600, 700, 800]) + [
            _row(model="gpt-5.5-mini", idle=i, hit=True) for i in (50, 90, 130)
        ]
        table = estimate_ttls(rows)
        # The mini model has no expiry evidence of its own -> no per-model key,
        # but its hits still feed the provider aggregate.
        assert "openai/gpt-5.5-mini" not in table
        assert table["openai"]["hits"] == 6


class TestWriteLearned:
    def test_merge_preserves_existing_keys(self, tmp_path):
        out = tmp_path / "learned.json"
        out.write_text(json.dumps({"kimi": {"ttl_seconds": 900}}))
        write_learned({"openai": {"ttl_seconds": 600}}, str(out))
        data = json.loads(out.read_text())
        assert data["kimi"]["ttl_seconds"] == 900
        assert data["openai"]["ttl_seconds"] == 600

    def test_corrupt_existing_file_is_replaced_not_fatal(self, tmp_path):
        out = tmp_path / "learned.json"
        out.write_text("{not json")
        write_learned({"openai": {"ttl_seconds": 600}}, str(out))
        assert json.loads(out.read_text())["openai"]["ttl_seconds"] == 600

    def test_non_dict_existing_file_is_replaced_not_merged(self, tmp_path):
        out = tmp_path / "learned.json"
        out.write_text("[1, 2]")
        write_learned({"openai": {"ttl_seconds": 600}}, str(out))
        assert json.loads(out.read_text()) == {"openai": {"ttl_seconds": 600}}


class TestMain:
    @pytest.fixture()
    def paths(self, tmp_path, monkeypatch):
        obs = tmp_path / "obs.jsonl"
        out = tmp_path / "learned.json"
        monkeypatch.setenv("HEADROOM_CACHE_TTL_OBS_PATH", str(obs))
        monkeypatch.setenv("HEADROOM_CACHE_TTL_LEARNED_PATH", str(out))
        return obs, out

    def test_end_to_end_resolve_learned_ttl_reads_output(self, paths):
        obs, out = paths
        rows = _corpus([120, 300, 480], [600, 900])
        rows += [_row(idle=650, hit=False)]
        # Trailing junk the parser must skip: invalid JSON, a blank line,
        # and valid JSON that is not an object.
        obs.write_text("\n".join(json.dumps(r) for r in rows) + '\nnot-json\n\n[1, 2]\n"scalar"\n')

        assert main([]) == 0
        assert resolve_learned_ttl("openai", "gpt-5.5") == 600
        assert resolve_learned_ttl("openai", "other-model") == 600  # provider fallback

    def test_no_estimable_data_writes_nothing(self, paths, capsys):
        obs, out = paths
        obs.write_text(json.dumps(_row(idle=120, hit=True)) + "\n")
        assert main([]) == 0
        assert not out.exists()
        assert "nothing written" in capsys.readouterr().out

    def test_missing_obs_file_is_not_an_error(self, paths):
        assert main([]) == 0

    def test_dry_run_prints_without_writing(self, paths, capsys):
        obs, out = paths
        obs.write_text("\n".join(json.dumps(r) for r in _corpus([120, 300, 480], [600, 900, 1200])))
        assert main(["--dry-run"]) == 0
        assert not out.exists()
        assert json.loads(capsys.readouterr().out)["openai"]["ttl_seconds"] == 600

    def test_explicit_paths_override_env(self, paths, tmp_path):
        obs2 = tmp_path / "other.jsonl"
        out2 = tmp_path / "other.json"
        obs2.write_text(
            "\n".join(json.dumps(r) for r in _corpus([120, 300, 480], [600, 900, 1200]))
        )
        assert main(["--obs", str(obs2), "--out", str(out2)]) == 0
        assert json.loads(out2.read_text())["openai"]["ttl_seconds"] == 600
