"""Offline cache-TTL estimator — the `headroom-cache-ttl` CLI.

The other half of the seam in :mod:`headroom.cache.ttl_observations`: the proxy
appends per-turn cache outcomes to ``cache_ttl_observations.jsonl`` (when
``HEADROOM_CACHE_TTL_LEARN`` is set); this batch tool reads those rows and
writes the ``cache_ttl_learned.json`` table that
:func:`~headroom.cache.ttl_observations.resolve_learned_ttl` consults, so cold
detection (``HEADROOM_COLD_RECOMPACT``) stops guessing for providers that don't
expose their TTL (Kimi / OpenAI / Codex).

Estimation is interval-based, per the contract in ``ttl_observations``: idle
gaps that still HIT bound the TTL from below (the cache was alive at that
idle), and ``ttl_expiry`` misses bound it from above (the cache was dead by
that idle). We emit the UPPER end of the interval — the smallest observed
death-idle beyond the largest observed life-idle — because the consumer treats
a turn as cold only when ``idle > ttl``: overestimating the TTL merely skips a
recompaction (lost savings), while underestimating it would recompact a
still-warm prefix and bust the cache (net cost). A key with no death evidence
beyond its last observed life is skipped entirely for the same reason.

Runs out-of-process on purpose (zero live-feedback risk on the hot path); the
proxy re-reads the learned table by mtime, so a periodic cron/launchd run is
enough. Never imports anything request-scoped.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from typing import Any

from headroom.cache.ttl_observations import _learned_path, _obs_path

DEFAULT_MIN_HITS = 3
DEFAULT_MIN_EXPIRY_MISSES = 3


def _iter_rows(path: str) -> list[dict[str, Any]]:
    """Parse the observation JSONL, skipping malformed lines (best-effort)."""
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def estimate_ttls(
    rows: list[dict[str, Any]],
    *,
    min_hits: int = DEFAULT_MIN_HITS,
    min_expiry_misses: int = DEFAULT_MIN_EXPIRY_MISSES,
) -> dict[str, dict[str, Any]]:
    """Per-key TTL estimates from raw observation rows.

    Keys follow the ``resolve_learned_ttl`` lookup order: ``provider/model``
    for every observed pair, plus a ``provider`` aggregate over all its models.
    A key is emitted only when it has both enough life evidence (``min_hits``
    hits with a positive idle) and enough death evidence (``min_expiry_misses``
    ``ttl_expiry`` misses), AND at least one death was observed at an idle
    larger than every observed life — otherwise the interval has no safe upper
    end and the static default is the better fallback.
    """
    hits: dict[str, list[float]] = defaultdict(list)
    expiries: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        model = row.get("model")
        keys = [provider]
        if isinstance(model, str) and model:
            keys.append(f"{provider}/{model}")
        try:
            idle = float(row.get("idle_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if idle <= 0:
            continue
        if row.get("is_miss"):
            if row.get("reason") == "ttl_expiry":
                for k in keys:
                    expiries[k].append(idle)
        else:
            for k in keys:
                hits[k].append(idle)

    now = round(time.time(), 3)
    table: dict[str, dict[str, Any]] = {}
    for key in sorted(set(hits) | set(expiries)):
        key_hits = hits.get(key, [])
        key_expiries = expiries.get(key, [])
        if len(key_hits) < min_hits or len(key_expiries) < min_expiry_misses:
            continue
        max_hit_idle = max(key_hits)
        deaths_after_life = [x for x in key_expiries if x > max_hit_idle]
        if not deaths_after_life:
            continue  # bounds overlap: no idle at which the cache is provably dead
        table[key] = {
            "ttl_seconds": int(min(deaths_after_life)),
            "max_hit_idle": int(max_hit_idle),
            "hits": len(key_hits),
            "ttl_expiry_misses": len(key_expiries),
            "updated_at": now,
        }
    return table


def write_learned(table: dict[str, dict[str, Any]], path: str) -> dict[str, Any]:
    """Merge ``table`` into the learned file at ``path`` (atomic tmp+rename).

    Merging (not replacing) keeps keys learned from an earlier, since-rotated
    observation window instead of silently un-learning them.
    """
    merged: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            merged.update(existing)
    except (OSError, json.JSONDecodeError):
        pass
    merged.update(table)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="headroom-cache-ttl",
        description=(
            "Estimate provider prompt-cache TTLs from cache_ttl_observations.jsonl "
            "and write cache_ttl_learned.json for the proxy's cold-prefix detection."
        ),
    )
    parser.add_argument(
        "--obs",
        default=None,
        help="Observation JSONL (default: $HEADROOM_CACHE_TTL_OBS_PATH or "
        "~/.headroom/cache_ttl_observations.jsonl)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Learned-table JSON to write (default: $HEADROOM_CACHE_TTL_LEARNED_PATH "
        "or ~/.headroom/cache_ttl_learned.json)",
    )
    parser.add_argument("--min-hits", type=int, default=DEFAULT_MIN_HITS)
    parser.add_argument("--min-expiry-misses", type=int, default=DEFAULT_MIN_EXPIRY_MISSES)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the estimate table without writing"
    )
    args = parser.parse_args(argv)

    obs_path = args.obs or _obs_path()
    out_path = args.out or _learned_path()
    rows = _iter_rows(obs_path)
    table = estimate_ttls(rows, min_hits=args.min_hits, min_expiry_misses=args.min_expiry_misses)
    if args.dry_run:
        print(json.dumps(table, indent=2, sort_keys=True))
        return 0
    if not table:
        print(
            f"headroom-cache-ttl: no estimable keys in {obs_path} "
            f"({len(rows)} rows); nothing written"
        )
        return 0
    merged = write_learned(table, out_path)
    print(f"headroom-cache-ttl: wrote {len(table)} estimate(s) ({len(merged)} total) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
