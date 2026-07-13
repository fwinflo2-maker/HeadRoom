"""Tests for local token/cache mode benchmark harness."""

from benchmarks.proxy_mode_benchmark import run_local_benchmark


def test_local_mode_benchmark_shows_compression_and_cache_tradeoff() -> None:
    results = run_local_benchmark(turns=6)

    baseline = results["baseline"]
    token = results["token"]
    cache = results["cache"]

    # TOKEN mode aggressively compresses aged tool_results. The fresh tail is
    # exempt (freshness exemption, issue #3), but earlier results still compress
    # once they age out of the freshness window, so it sends far fewer tokens
    # than baseline.
    assert token.total_tokens_saved > 0
    assert token.total_sent_tokens < baseline.total_sent_tokens

    # CACHE mode's live zone is only the final message, and the freshness
    # exemption (issue #3) protects a fresh tail tool_result there — so CACHE
    # mode does not compress tool output. Its value is prefix stability: send
    # each result verbatim once, then keep it byte-identical in the frozen
    # prefix so the provider serves it from cache on every later turn. It
    # therefore sends more tokens than TOKEN mode but preserves the cached
    # prefix better — the exact tradeoff this benchmark exists to surface.
    # (Before the issue #3 fix, CACHE mode's only compression came from
    # rewriting that fresh tail — the very anti-pattern the fix removes — so
    # its former `total_tokens_saved > 0` was encoding buggy behavior.)
    assert cache.total_sent_tokens <= baseline.total_sent_tokens
    assert cache.total_sent_tokens >= token.total_sent_tokens
    assert cache.total_cache_read_tokens >= token.total_cache_read_tokens
